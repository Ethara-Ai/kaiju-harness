//! Byte-span surgery: walk the AST read-only, collect `(byte_range, replacement)`
//! edits for function bodies (and optionally doc attributes), then splice them
//! into the ORIGINAL source string in reverse offset order.
//!
//! Everything outside the collected byte ranges is byte-identical to the input.
//! This is what makes the stubber preserve macros, blank lines, inline comments,
//! and arbitrary whitespace verbatim across any future Rust repo.

use std::collections::BTreeSet;
use std::ops::Range;
use syn::spanned::Spanned;
use syn::visit::{self, Visit};
use syn::{File, ImplItemFn, ItemFn, ItemImpl, ItemMod, Macro, TraitItemFn};

use crate::cfg_test::{has_ctor_or_dtor_attr, has_proc_macro_attr, has_test_attr, has_test_gate};
use crate::macro_recurse::{is_macro_definition, parse_macro_items};

#[derive(Debug, Clone)]
pub struct Edit {
    pub range: Range<usize>,
    pub replacement: String,
}

#[derive(Default, Clone, Copy, Debug)]
pub struct CollectOptions {
    pub strip_docs: bool,
}

#[derive(Default, Debug)]
pub struct CollectReport {
    pub edits: Vec<Edit>,
    pub unknown_macros_skipped: BTreeSet<String>,
}

pub fn collect_edits(file: &File, opts: CollectOptions) -> Vec<Edit> {
    collect_edits_with_report(file, opts, file_source_len_hint(file)).edits
}

pub fn collect_edits_with_report(
    file: &File,
    opts: CollectOptions,
    source_len: usize,
) -> CollectReport {
    let mut collector = EditCollector {
        edits: Vec::new(),
        opts,
        source_len,
        unknown_macros_skipped: BTreeSet::new(),
    };
    if opts.strip_docs {
        for attr in &file.attrs {
            if let Some(edit) = doc_attr_edit_bounded(attr, source_len) {
                collector.edits.push(edit);
            }
        }
    }
    visit::visit_file(&mut collector, file);
    CollectReport {
        edits: prune_contained(collector.edits),
        unknown_macros_skipped: collector.unknown_macros_skipped,
    }
}

fn file_source_len_hint(_file: &File) -> usize {
    usize::MAX
}

pub fn apply_edits(source: &str, mut edits: Vec<Edit>) -> String {
    edits.sort_by_key(|e| e.range.start);
    let mut out = source.to_string();
    for e in edits.iter().rev() {
        if e.range.end > out.len() {
            continue;
        }
        if !out.is_char_boundary(e.range.start) || !out.is_char_boundary(e.range.end) {
            continue;
        }
        out.replace_range(e.range.clone(), &e.replacement);
    }
    out
}

pub fn strip_comments(source: &str) -> String {
    let bytes = source.as_bytes();
    let mut out = String::with_capacity(source.len());
    let mut i = 0;
    while i < bytes.len() {
        let b = bytes[i];

        if b >= 0x80 {
            let start = i;
            while i < bytes.len() && bytes[i] >= 0x80 {
                i += 1;
            }
            out.push_str(&source[start..i]);
            continue;
        }

        if b == b'"' {
            let (end, slice) = scan_string_literal(bytes, i);
            out.push_str(slice);
            i = end;
            continue;
        }
        if b == b'\'' {
            if let Some((end, slice)) = try_scan_char_literal(source, bytes, i) {
                out.push_str(slice);
                i = end;
                continue;
            }
        }
        if b == b'r' && i + 1 < bytes.len() && (bytes[i + 1] == b'"' || bytes[i + 1] == b'#') {
            if let Some((end, slice)) = try_scan_raw_string(source, bytes, i) {
                out.push_str(slice);
                i = end;
                continue;
            }
        }
        if b == b'b' && i + 1 < bytes.len() {
            let nb = bytes[i + 1];
            if nb == b'"' {
                let (end, slice) = scan_string_literal(bytes, i + 1);
                out.push(b as char);
                out.push_str(slice);
                i = end;
                continue;
            }
            if nb == b'\'' {
                if let Some((end, slice)) = try_scan_char_literal(source, bytes, i + 1) {
                    out.push(b as char);
                    out.push_str(slice);
                    i = end;
                    continue;
                }
            }
        }

        if b == b'#' {
            if let Some(end) = try_scan_doc_attr(bytes, i) {
                i = end;
                continue;
            }
        }

        if b == b'/' && i + 1 < bytes.len() {
            let nb = bytes[i + 1];
            if nb == b'/' {
                let line_end = memchr_newline(bytes, i + 2);
                i = line_end;
                continue;
            }
            if nb == b'*' {
                if let Some(end) = scan_block_comment(bytes, i + 2) {
                    i = end;
                    continue;
                }
            }
        }

        out.push(b as char);
        i += 1;
    }
    collapse_blank_runs(&out)
}

fn try_scan_doc_attr(bytes: &[u8], start: usize) -> Option<usize> {
    let mut i = start + 1;
    if i < bytes.len() && bytes[i] == b'!' {
        i += 1;
    }
    if i >= bytes.len() || bytes[i] != b'[' {
        return None;
    }
    i += 1;
    while i < bytes.len() && bytes[i].is_ascii_whitespace() {
        i += 1;
    }
    if i + 3 > bytes.len() || &bytes[i..i + 3] != b"doc" {
        return None;
    }
    i += 3;
    while i < bytes.len() && bytes[i].is_ascii_whitespace() {
        i += 1;
    }
    if i >= bytes.len() {
        return None;
    }
    let first = bytes[i];
    if first != b'=' && first != b'(' {
        return None;
    }
    let mut bracket_depth = 1usize;
    while i < bytes.len() && bracket_depth > 0 {
        let c = bytes[i];
        if c == b'"' {
            i += 1;
            while i < bytes.len() {
                if bytes[i] == b'\\' && i + 1 < bytes.len() {
                    i += 2;
                    continue;
                }
                if bytes[i] == b'"' {
                    i += 1;
                    break;
                }
                i += 1;
            }
            continue;
        }
        if c == b'[' {
            bracket_depth += 1;
        } else if c == b']' {
            bracket_depth -= 1;
            if bracket_depth == 0 {
                i += 1;
                break;
            }
        }
        i += 1;
    }
    Some(i)
}

fn scan_string_literal<'a>(bytes: &'a [u8], start: usize) -> (usize, &'a str) {
    let mut i = start + 1;
    while i < bytes.len() {
        let b = bytes[i];
        if b == b'\\' && i + 1 < bytes.len() {
            i += 2;
            continue;
        }
        if b == b'"' {
            i += 1;
            break;
        }
        i += 1;
    }
    let slice = std::str::from_utf8(&bytes[start..i])
        .expect("rust source is valid utf8 and string literals are byte-aligned");
    (i, slice)
}

fn try_scan_char_literal<'a>(
    source: &'a str,
    bytes: &'a [u8],
    start: usize,
) -> Option<(usize, &'a str)> {
    if start + 1 < bytes.len() {
        let next = bytes[start + 1];
        if next.is_ascii_alphabetic() || next == b'_' {
            let mut j = start + 1;
            while j < bytes.len() {
                let c = bytes[j];
                if c.is_ascii_alphanumeric() || c == b'_' {
                    j += 1;
                } else {
                    break;
                }
            }
            if j == bytes.len() || bytes[j] != b'\'' {
                return None;
            }
        }
    }
    let mut i = start + 1;
    while i < bytes.len() {
        let b = bytes[i];
        if b == b'\\' && i + 1 < bytes.len() {
            i += 2;
            continue;
        }
        if b == b'\'' {
            i += 1;
            break;
        }
        i += 1;
    }
    if i > bytes.len() {
        return None;
    }
    let slice = &source[start..i];
    Some((i, slice))
}

fn try_scan_raw_string<'a>(
    source: &'a str,
    bytes: &'a [u8],
    start: usize,
) -> Option<(usize, &'a str)> {
    let mut i = start + 1;
    let mut hash_count = 0usize;
    while i < bytes.len() && bytes[i] == b'#' {
        hash_count += 1;
        i += 1;
    }
    if i >= bytes.len() || bytes[i] != b'"' {
        return None;
    }
    i += 1;
    loop {
        if i >= bytes.len() {
            return None;
        }
        if bytes[i] == b'"' {
            let mut j = i + 1;
            let mut matched = 0;
            while matched < hash_count && j < bytes.len() && bytes[j] == b'#' {
                matched += 1;
                j += 1;
            }
            if matched == hash_count {
                let end = j;
                return Some((end, &source[start..end]));
            }
        }
        i += 1;
    }
}

fn scan_block_comment(bytes: &[u8], start: usize) -> Option<usize> {
    let mut depth = 1usize;
    let mut i = start;
    while i + 1 < bytes.len() {
        if bytes[i] == b'/' && bytes[i + 1] == b'*' {
            depth += 1;
            i += 2;
            continue;
        }
        if bytes[i] == b'*' && bytes[i + 1] == b'/' {
            depth -= 1;
            i += 2;
            if depth == 0 {
                return Some(i);
            }
            continue;
        }
        i += 1;
    }
    None
}

fn memchr_newline(bytes: &[u8], start: usize) -> usize {
    let mut i = start;
    while i < bytes.len() && bytes[i] != b'\n' {
        i += 1;
    }
    i
}

fn collapse_blank_runs(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut blank_run = 0usize;
    for line in s.split_inclusive('\n') {
        let trimmed_end = line.trim_end_matches('\n');
        if trimmed_end.chars().all(|c| c.is_whitespace()) {
            blank_run += 1;
            if blank_run <= 1 {
                out.push_str(line);
            }
        } else {
            blank_run = 0;
            out.push_str(line);
        }
    }
    out
}

pub fn prune_contained(mut edits: Vec<Edit>) -> Vec<Edit> {
    edits.sort_by_key(|e| (e.range.start, std::cmp::Reverse(e.range.end)));
    let mut out: Vec<Edit> = Vec::with_capacity(edits.len());
    for e in edits {
        match out.last() {
            Some(prev) if e.range.start >= prev.range.start && e.range.end <= prev.range.end => {
                continue;
            }
            _ => out.push(e),
        }
    }
    out
}

struct EditCollector {
    edits: Vec<Edit>,
    opts: CollectOptions,
    source_len: usize,
    unknown_macros_skipped: BTreeSet<String>,
}

impl EditCollector {
    fn maybe_collect_fn_body(
        &mut self,
        sig: &syn::Signature,
        attrs: &[syn::Attribute],
        block: &syn::Block,
    ) {
        if should_skip_fn(sig, attrs) {
            return;
        }
        let range = block.span().byte_range();
        if !self.range_is_real(&range) {
            return;
        }
        self.edits.push(Edit {
            range,
            replacement: r#"{ panic!("STUB: not implemented") }"#.to_string(),
        });
    }

    fn maybe_collect_closure_body(&mut self, closure: &syn::ExprClosure) {
        let body_range = closure.body.span().byte_range();
        if !self.range_is_real(&body_range) {
            return;
        }
        self.edits.push(Edit {
            range: body_range,
            replacement: r#"{ panic!("STUB: not implemented") }"#.to_string(),
        });
    }

    fn range_is_real(&self, range: &Range<usize>) -> bool {
        if range.start == 0 && range.end == 0 {
            return false;
        }
        if range.end > self.source_len {
            return false;
        }
        range.start < range.end
    }

    fn maybe_strip_docs(&mut self, attrs: &[syn::Attribute]) {
        if !self.opts.strip_docs {
            return;
        }
        for attr in attrs {
            if let Some(edit) = doc_attr_edit_bounded(attr, self.source_len) {
                self.edits.push(edit);
            }
        }
    }
}

impl<'ast> Visit<'ast> for EditCollector {
    fn visit_item_fn(&mut self, node: &'ast ItemFn) {
        self.maybe_strip_docs(&node.attrs);
        self.maybe_collect_fn_body(&node.sig, &node.attrs, &node.block);
    }

    fn visit_impl_item_fn(&mut self, node: &'ast ImplItemFn) {
        self.maybe_strip_docs(&node.attrs);
        self.maybe_collect_fn_body(&node.sig, &node.attrs, &node.block);
    }

    fn visit_trait_item_fn(&mut self, node: &'ast TraitItemFn) {
        self.maybe_strip_docs(&node.attrs);
        if let Some(block) = &node.default {
            self.maybe_collect_fn_body(&node.sig, &node.attrs, block);
        }
    }

    fn visit_item_mod(&mut self, node: &'ast ItemMod) {
        if has_test_gate(&node.attrs) {
            return;
        }
        self.maybe_strip_docs(&node.attrs);
        visit::visit_item_mod(self, node);
    }

    fn visit_item_impl(&mut self, node: &'ast ItemImpl) {
        self.maybe_strip_docs(&node.attrs);
        visit::visit_item_impl(self, node);
    }

    fn visit_item_macro(&mut self, node: &'ast syn::ItemMacro) {
        if is_macro_definition(node) {
            return;
        }
        self.maybe_strip_docs(&node.attrs);
        self.visit_macro(&node.mac);
    }

    fn visit_macro(&mut self, node: &'ast Macro) {
        if let Some(items) = parse_macro_items(node) {
            for item in &items {
                self.visit_item(item);
            }
        } else if let Some(name) = node.path.get_ident().map(|i| i.to_string()) {
            if !STUB_SAFE_SKIPPED_MACROS.contains(&name.as_str()) {
                self.unknown_macros_skipped.insert(name);
            }
        }
    }

    fn visit_item_const(&mut self, node: &'ast syn::ItemConst) {
        self.maybe_strip_docs(&node.attrs);
        visit::visit_expr(self, &node.expr);
    }
    fn visit_item_static(&mut self, node: &'ast syn::ItemStatic) {
        self.maybe_strip_docs(&node.attrs);
        visit::visit_expr(self, &node.expr);
    }

    fn visit_expr_closure(&mut self, node: &'ast syn::ExprClosure) {
        self.maybe_collect_closure_body(node);
    }

    fn visit_item_struct(&mut self, node: &'ast syn::ItemStruct) {
        self.maybe_strip_docs(&node.attrs);
        visit::visit_item_struct(self, node);
    }
    fn visit_item_enum(&mut self, node: &'ast syn::ItemEnum) {
        self.maybe_strip_docs(&node.attrs);
        visit::visit_item_enum(self, node);
    }
    fn visit_item_union(&mut self, node: &'ast syn::ItemUnion) {
        self.maybe_strip_docs(&node.attrs);
        visit::visit_item_union(self, node);
    }
    fn visit_item_trait(&mut self, node: &'ast syn::ItemTrait) {
        self.maybe_strip_docs(&node.attrs);
        visit::visit_item_trait(self, node);
    }
    fn visit_item_type(&mut self, node: &'ast syn::ItemType) {
        self.maybe_strip_docs(&node.attrs);
    }
    fn visit_item_use(&mut self, node: &'ast syn::ItemUse) {
        self.maybe_strip_docs(&node.attrs);
    }
    fn visit_item_extern_crate(&mut self, node: &'ast syn::ItemExternCrate) {
        self.maybe_strip_docs(&node.attrs);
    }
    fn visit_field(&mut self, node: &'ast syn::Field) {
        self.maybe_strip_docs(&node.attrs);
    }
    fn visit_variant(&mut self, node: &'ast syn::Variant) {
        self.maybe_strip_docs(&node.attrs);
    }
    fn visit_trait_item_const(&mut self, node: &'ast syn::TraitItemConst) {
        self.maybe_strip_docs(&node.attrs);
    }
    fn visit_trait_item_type(&mut self, node: &'ast syn::TraitItemType) {
        self.maybe_strip_docs(&node.attrs);
    }
    fn visit_impl_item_const(&mut self, node: &'ast syn::ImplItemConst) {
        self.maybe_strip_docs(&node.attrs);
    }
    fn visit_impl_item_type(&mut self, node: &'ast syn::ImplItemType) {
        self.maybe_strip_docs(&node.attrs);
    }
    fn visit_item_foreign_mod(&mut self, node: &'ast syn::ItemForeignMod) {
        self.maybe_strip_docs(&node.attrs);
        for it in &node.items {
            match it {
                syn::ForeignItem::Fn(f) => self.maybe_strip_docs(&f.attrs),
                syn::ForeignItem::Static(s) => self.maybe_strip_docs(&s.attrs),
                syn::ForeignItem::Type(t) => self.maybe_strip_docs(&t.attrs),
                syn::ForeignItem::Macro(m) => self.maybe_strip_docs(&m.attrs),
                _ => {}
            }
        }
    }
}

fn should_skip_fn(sig: &syn::Signature, attrs: &[syn::Attribute]) -> bool {
    let name = sig.ident.to_string();
    name == "main"
        || sig.constness.is_some()
        || has_test_attr(attrs)
        || has_test_gate(attrs)
        || has_proc_macro_attr(attrs)
        || has_ctor_or_dtor_attr(attrs)
        || return_type_has_impl_trait(sig)
}

/// A function returning return-position `impl Trait` (RPIT) — at the top level
/// OR nested (e.g. `Result<impl Iterator, E>`, `Option<impl Trait>`) — CANNOT be
/// stubbed with a divergent body. `impl Trait` is an opaque type that needs a
/// concrete *defining use*; rustc infers the hidden type of `{ panic!() }` /
/// `{ loop {} }` as `()`, which doesn't implement the trait, so the stubbed base
/// fails to compile (`error[E0277]: () is not an iterator`). There is no
/// trait-agnostic body that satisfies an arbitrary opaque return, so we LEAVE
/// these functions unstubbed (their real body is retained) to keep the base
/// compilable. This is a deliberate, narrow leak — RPIT functions are usually a
/// small fraction of a crate — traded for a valid, buildable task.
fn return_type_has_impl_trait(sig: &syn::Signature) -> bool {
    struct ImplTraitFinder {
        found: bool,
    }
    impl<'ast> Visit<'ast> for ImplTraitFinder {
        fn visit_type_impl_trait(&mut self, _node: &'ast syn::TypeImplTrait) {
            self.found = true;
        }
    }
    match &sig.output {
        syn::ReturnType::Type(_, ty) => {
            let mut finder = ImplTraitFinder { found: false };
            visit::visit_type(&mut finder, ty);
            finder.found
        }
        syn::ReturnType::Default => false,
    }
}

fn doc_attr_edit_bounded(attr: &syn::Attribute, source_len: usize) -> Option<Edit> {
    if !attr.path().is_ident("doc") {
        return None;
    }
    let r = attr.span().byte_range();
    if r.start == 0 && r.end == 0 {
        return None;
    }
    if r.end > source_len || r.start >= r.end {
        return None;
    }
    Some(Edit { range: r, replacement: String::new() })
}

const STUB_SAFE_SKIPPED_MACROS: &[&str] = &[
    "println", "print", "eprintln", "eprint",
    "format", "write", "writeln",
    "vec", "matches", "assert", "assert_eq", "assert_ne",
    "debug_assert", "debug_assert_eq", "debug_assert_ne",
    "panic", "todo", "unimplemented", "unreachable",
    "dbg", "stringify", "concat", "include_str", "include_bytes",
    "env", "option_env", "file", "line", "column", "module_path",
    "thread_local",
];

#[cfg(test)]
mod tests {
    use super::*;

    fn stub(src: &str) -> String {
        let file: File = syn::parse_str(src).expect("parse");
        let edits = collect_edits(&file, CollectOptions::default());
        apply_edits(src, edits)
    }

    fn stub_strip_docs(src: &str) -> String {
        let file: File = syn::parse_str(src).expect("parse");
        let edits = collect_edits(&file, CollectOptions { strip_docs: true });
        apply_edits(src, edits)
    }

    #[test]
    fn normal_fn_body_is_stubbed() {
        let src = "fn foo() { real_impl(); }\n";
        let out = stub(src);
        assert_eq!(out, "fn foo() { panic!(\"STUB: not implemented\") }\n");
    }

    #[test]
    fn const_fn_preserved() {
        let src = "const fn foo() -> i32 { 42 }\n";
        assert_eq!(stub(src), src);
    }

    #[test]
    fn fn_main_preserved() {
        let src = "fn main() { println!(\"hi\"); }\n";
        assert_eq!(stub(src), src);
    }

    #[test]
    fn test_attr_fn_preserved() {
        let src = "#[test]\nfn t() { assert!(true); }\n";
        assert_eq!(stub(src), src);
    }

    #[test]
    fn cfg_test_fn_preserved() {
        let src = "#[cfg(test)]\nfn t() { panic!(); }\n";
        assert_eq!(stub(src), src);
    }

    #[test]
    fn cfg_test_mod_preserved_entirely() {
        let src = "#[cfg(test)]\nmod tests {\n    fn inside() { real(); }\n}\n";
        assert_eq!(stub(src), src);
    }

    #[test]
    fn cfg_test_utils_is_not_preserved_and_gets_stubbed() {
        let src = "#[cfg(feature = \"test-utils\")]\nfn helper() { real(); }\n";
        let out = stub(src);
        assert!(out.contains(r#"fn helper() { panic!("STUB: not implemented") }"#), "got: {}", out);
    }

    #[test]
    fn cfg_loom_fn_preserved() {
        let src = "#[cfg(loom)]\nfn loom_only() { real(); }\n";
        assert_eq!(stub(src), src);
    }

    #[test]
    fn impl_method_body_stubbed() {
        let src = "impl Foo {\n    pub fn bar(&self) -> i32 { 42 }\n}\n";
        let out = stub(src);
        assert!(out.contains(r#"pub fn bar(&self) -> i32 { panic!("STUB: not implemented") }"#), "got: {}", out);
    }

    #[test]
    fn trait_decl_no_body_unchanged() {
        let src = "trait T {\n    fn foo(&self) -> i32;\n}\n";
        assert_eq!(stub(src), src);
    }

    #[test]
    fn trait_default_method_is_stubbed() {
        let src = "trait T {\n    fn foo(&self) -> i32 { 42 }\n}\n";
        let out = stub(src);
        assert!(out.contains(r#"fn foo(&self) -> i32 { panic!("STUB: not implemented") }"#), "got: {}", out);
    }

    #[test]
    fn async_fn_stubbed() {
        let src = "async fn foo() -> Result<(), ()> { real().await }\n";
        let out = stub(src);
        assert!(out.contains(r#"async fn foo() -> Result<(), ()> { panic!("STUB: not implemented") }"#), "got: {}", out);
    }

    #[test]
    fn unsafe_fn_stubbed() {
        let src = "unsafe fn raw() -> *const u8 { real() }\n";
        let out = stub(src);
        assert!(out.contains(r#"unsafe fn raw() -> *const u8 { panic!("STUB: not implemented") }"#), "got: {}", out);
    }

    #[test]
    fn impl_trait_return_left_unstubbed_so_base_compiles() {
        // RPIT (return-position impl Trait) cannot be stubbed with a divergent
        // body — the opaque return type needs a concrete defining use, so
        // `{ panic!() }` infers the hidden type as `()` and fails to compile
        // (`error[E0277]: () is not an iterator`). We therefore leave the real
        // body intact rather than emit a non-compiling stub.
        let src = "fn it() -> impl Iterator<Item=i32> { vec![1,2,3].into_iter() }\n";
        let out = stub(src);
        assert!(out.contains("vec![1,2,3].into_iter()"), "real body must be retained; got: {}", out);
        assert!(!out.contains("panic!"), "must NOT emit a non-compiling stub; got: {}", out);
    }

    #[test]
    fn nested_impl_trait_return_also_left_unstubbed() {
        // impl Trait nested inside Result/Option/Box/tuple has the same problem.
        for ret in [
            "Result<impl Iterator<Item=i32>, ()>",
            "Option<impl Iterator<Item=i32>>",
            "Box<impl Iterator<Item=i32>>",
            "(impl Iterator<Item=i32>, i32)",
        ] {
            let src = format!("fn it() -> {ret} {{ real_body() }}\n");
            let out = stub(&src);
            assert!(out.contains("real_body()"), "body must be retained for {ret}; got: {out}");
            assert!(!out.contains("panic!"), "must not stub RPIT return {ret}; got: {out}");
        }
    }

    #[test]
    fn dyn_trait_return_is_still_stubbed() {
        // `Box<dyn Trait>` is a concrete type — a divergent stub compiles fine,
        // so it SHOULD still be stubbed (don't over-skip).
        let src = "fn it() -> Box<dyn Iterator<Item=i32>> { real_body() }\n";
        let out = stub(src);
        assert!(out.contains(r#"panic!("STUB: not implemented")"#), "dyn return should be stubbed; got: {out}");
        assert!(!out.contains("real_body()"), "real body must be removed; got: {out}");
    }

    #[test]
    fn fn_inside_cfg_rt_macro_is_stubbed() {
        let src = "cfg_rt! {\n    pub fn spawn() {\n        real_impl();\n    }\n}\n";
        let out = stub(src);
        assert!(out.contains(r#"pub fn spawn() { panic!("STUB: not implemented") }"#), "got:\n{}", out);
        assert!(!out.contains("real_impl"), "real body must NOT leak; got:\n{}", out);
    }

    #[test]
    fn fn_inside_nested_macros_is_stubbed() {
        let src = "cfg_rt! {\n    cfg_fs! {\n        pub fn nested() {\n            real();\n        }\n    }\n}\n";
        let out = stub(src);
        assert!(out.contains(r#"pub fn nested() { panic!("STUB: not implemented") }"#), "got:\n{}", out);
        assert!(!out.contains("real()"), "real body must NOT leak; got:\n{}", out);
    }

    #[test]
    fn macro_definition_left_verbatim() {
        let src = "macro_rules! my_macro {\n    () => { fn never_called() { real() } };\n}\n";
        assert_eq!(stub(src), src);
    }

    #[test]
    fn blank_lines_outside_fn_bodies_preserved() {
        let src = "use foo;\n\n\nfn a() { x() }\n\n\nfn b() { y() }\n\n\nuse bar;\n";
        let out = stub(src);
        assert!(out.starts_with("use foo;\n\n\n"), "blank lines lost; got:\n{}", out);
        let stub_count = out.matches(r#"panic!("STUB: not implemented")"#).count();
        assert_eq!(stub_count, 2, "expected exactly 2 stubs; got:\n{}", out);
        assert!(out.contains("\n\n\n"), "blank lines around stubs lost; got:\n{}", out);
        assert!(out.ends_with("\n\n\nuse bar;\n"), "trailing blank lines lost; got:\n{}", out);
    }

    #[test]
    fn inline_comment_between_items_preserved() {
        let src = "// pre comment\nfn a() { real() }\n// mid comment\nfn b() { real() }\n// tail\n";
        let out = stub(src);
        let stub = r#"panic!("STUB: not implemented")"#;
        let expected = format!(
            "// pre comment\nfn a() {{ {stub} }}\n// mid comment\nfn b() {{ {stub} }}\n// tail\n"
        );
        assert_eq!(out, expected);
    }

    #[test]
    fn doc_comment_outside_macro_preserved() {
        let src = "/// docline\nfn a() { real() }\n";
        assert_eq!(stub(src), "/// docline\nfn a() { panic!(\"STUB: not implemented\") }\n");
    }

    #[test]
    fn proc_macro_attr_fn_preserved() {
        let src = "#[proc_macro]\nfn x(_: TokenStream) -> TokenStream { real() }\n";
        assert_eq!(stub(src), src);
    }

    #[test]
    fn nested_inner_fn_does_not_double_stub() {
        let src = "fn outer() { fn inner() { real() } inner() }\n";
        let out = stub(src);
        assert!(out.contains(r#"fn outer() { panic!("STUB: not implemented") }"#), "got:\n{}", out);
        let count = out.matches(r#"panic!("STUB: not implemented")"#).count();
        assert_eq!(count, 1, "should be exactly one stub, not nested; got:\n{}", out);
    }

    #[test]
    fn strip_docs_removes_outer_doc_comments() {
        let src = "/// a doc\n/// continued\nfn foo() { real() }\n";
        let out = stub_strip_docs(src);
        assert!(!out.contains("/// a doc"), "doc still there; got:\n{}", out);
        assert!(!out.contains("/// continued"), "got:\n{}", out);
        assert!(out.contains(r#"fn foo() { panic!("STUB: not implemented") }"#), "got:\n{}", out);
    }

    #[test]
    fn strip_docs_off_preserves_docs() {
        let src = "/// a doc\nfn foo() { real() }\n";
        let out = stub(src);
        assert!(out.contains("/// a doc"), "got:\n{}", out);
    }

    #[test]
    fn extern_c_fn_with_body_stubbed() {
        let src = "extern \"C\" fn ffi() -> i32 { 0 }\n";
        let out = stub(src);
        assert!(out.contains(r#"{ panic!("STUB: not implemented") }"#), "got:\n{}", out);
    }

    #[test]
    fn impl_for_type_in_macro_methods_stubbed() {
        let src = "cfg_rt! {\n    impl Foo for Bar {\n        fn poll(&self) -> i32 { real() }\n    }\n}\n";
        let out = stub(src);
        assert!(out.contains(r#"fn poll(&self) -> i32 { panic!("STUB: not implemented") }"#), "got:\n{}", out);
        assert!(!out.contains("real()"), "real body must NOT leak; got:\n{}", out);
    }

    #[test]
    fn outside_fn_body_bytes_identical_simple_case() {
        let src = "fn a() { real() }\n";
        let out = stub(src);
        let before_fn_a = "fn a() ";
        assert!(out.starts_with(before_fn_a), "prefix changed; got:\n{}", out);
        assert!(out.ends_with("\n"), "trailing newline lost");
    }

    #[test]
    fn prune_contained_keeps_only_maximal() {
        let edits = vec![
            Edit { range: 10..30, replacement: "outer".into() },
            Edit { range: 15..25, replacement: "inner".into() },
            Edit { range: 50..60, replacement: "later".into() },
        ];
        let pruned = prune_contained(edits);
        assert_eq!(pruned.len(), 2);
        assert_eq!(pruned[0].replacement, "outer");
        assert_eq!(pruned[1].replacement, "later");
    }

    #[test]
    fn apply_edits_reverse_offset_safe() {
        let src = "0123456789";
        let edits = vec![
            Edit { range: 2..4, replacement: "X".into() },
            Edit { range: 6..8, replacement: "Y".into() },
        ];
        assert_eq!(apply_edits(src, edits), "01X45Y89");
    }

    #[test]
    fn strip_comments_removes_line_comments() {
        let src = "use foo; // TODO: fix\nfn x() { real(); } // SAFETY: ok\n";
        let out = strip_comments(src);
        assert!(!out.contains("TODO"), "got: {out:?}");
        assert!(!out.contains("SAFETY"), "got: {out:?}");
        assert!(out.contains("use foo;"));
        assert!(out.contains("fn x()"));
    }

    #[test]
    fn strip_comments_removes_block_comments() {
        let src = "fn x() { /* explain it */ real(); }\n";
        let out = strip_comments(src);
        assert!(!out.contains("explain it"), "got: {out:?}");
        assert!(out.contains("fn x()"));
    }

    #[test]
    fn strip_comments_handles_nested_block_comments() {
        let src = "fn x() { /* outer /* inner */ still outer */ real(); }\n";
        let out = strip_comments(src);
        assert!(!out.contains("outer"), "got: {out:?}");
        assert!(!out.contains("inner"), "got: {out:?}");
        assert!(out.contains("real()"));
    }

    #[test]
    fn strip_comments_preserves_url_in_string_literal() {
        let src = r#"let s = "http://example.com/path"; // strip this comment
"#;
        let out = strip_comments(src);
        assert!(out.contains("http://example.com/path"), "URL inside string lost: {out:?}");
        assert!(!out.contains("strip this comment"), "comment kept: {out:?}");
    }

    #[test]
    fn strip_comments_preserves_slash_inside_char_literal() {
        let src = "let c = '/'; // gone\n";
        let out = strip_comments(src);
        assert!(out.contains("'/'"), "char literal damaged: {out:?}");
        assert!(!out.contains("gone"), "comment kept: {out:?}");
    }

    #[test]
    fn strip_comments_preserves_lifetime_apostrophes() {
        let src = "fn f<'a>(x: &'a str) -> &'a str { panic!() }\n";
        let out = strip_comments(src);
        assert_eq!(out, src, "lifetime apostrophes treated as char literal");
    }

    #[test]
    fn strip_comments_preserves_raw_string_with_slashes() {
        let src = r####"let r = r#"// not a comment /* nor this */"#; // but this is
"####;
        let out = strip_comments(src);
        assert!(out.contains(r##"r#"// not a comment /* nor this */"#"##), "raw string broken: {out:?}");
        assert!(!out.contains("but this is"), "real line comment kept: {out:?}");
    }

    #[test]
    fn strip_comments_removes_outer_doc_block() {
        let src = "/** outer doc */\nfn f() { real() }\n";
        let out = strip_comments(src);
        assert!(!out.contains("outer doc"), "got: {out:?}");
        assert!(out.contains("fn f()"));
    }

    #[test]
    fn strip_comments_removes_inner_doc_block() {
        let src = "/*! inner mod doc */\nuse foo;\n";
        let out = strip_comments(src);
        assert!(!out.contains("inner mod doc"), "got: {out:?}");
        assert!(out.contains("use foo;"));
    }

    #[test]
    fn strip_comments_collapses_consecutive_blank_lines() {
        let src = "use a;\n\n\n\nuse b;\n";
        let out = strip_comments(src);
        let blank_runs = out.matches("\n\n\n").count();
        assert_eq!(blank_runs, 0, "more than 1 consecutive blank line: {out:?}");
        assert!(out.contains("use a;"));
        assert!(out.contains("use b;"));
    }

    #[test]
    fn strip_comments_full_strip_docs_pipeline_e2e() {
        let src = "//! inner module doc\n/// doc comment\n/// continued\nfn f() { /* explain */ real(); } // tail\n";
        let stubbed = stub(src);
        let stripped = strip_comments(&stubbed);
        assert!(!stripped.contains("doc comment"), "doc lost: {stripped:?}");
        assert!(!stripped.contains("continued"), "got: {stripped:?}");
        assert!(!stripped.contains("explain"), "got: {stripped:?}");
        assert!(!stripped.contains("tail"), "got: {stripped:?}");
        assert!(!stripped.contains("inner module doc"), "got: {stripped:?}");
        assert!(stripped.contains("fn f()"));
        assert!(stripped.contains("panic!(\"STUB: not implemented\")"));
    }

    #[test]
    fn strip_comments_byte_string_literal() {
        let src = r#"let bs = b"foo // bar"; // strip me
"#;
        let out = strip_comments(src);
        assert!(out.contains(r#"b"foo // bar""#), "byte string broken: {out:?}");
        assert!(!out.contains("strip me"), "comment kept: {out:?}");
    }

    #[test]
    fn strip_comments_removes_doc_attr_inside_macro_rules() {
        let src = "macro_rules! gen {\n    () => {\n        #[doc = \"# Safety\"]\n        #[doc = \"\"]\n        #[doc = \"must be valid\"]\n        pub fn x() {}\n    };\n}\n";
        let out = strip_comments(src);
        assert!(!out.contains("# Safety"), "doc attr leaked: {out:?}");
        assert!(!out.contains("must be valid"), "doc attr leaked: {out:?}");
        assert!(out.contains("pub fn x()"), "fn signature lost: {out:?}");
        assert!(out.contains("macro_rules!"), "macro decl lost: {out:?}");
    }

    #[test]
    fn strip_comments_removes_inner_doc_attr() {
        let src = "mod m {\n    #![doc = \"module doc as attr\"]\n    pub fn x() {}\n}\n";
        let out = strip_comments(src);
        assert!(!out.contains("module doc as attr"), "inner doc attr leaked: {out:?}");
        assert!(out.contains("pub fn x()"));
    }

    #[test]
    fn strip_comments_preserves_utf8_identifiers_and_strings() {
        let src = "fn \u{65e5}\u{672c}\u{8a9e}() { let s = \"\u{3053}\u{3093}\u{306b}\u{3061}\u{306f}\"; } // strip\n";
        let out = strip_comments(src);
        assert!(out.contains("\u{65e5}\u{672c}\u{8a9e}"), "Japanese ident lost: {out:?}");
        assert!(out.contains("\u{3053}\u{3093}\u{306b}\u{3061}\u{306f}"), "Japanese string lost: {out:?}");
        assert!(!out.contains("strip"), "comment kept: {out:?}");
    }

    #[test]
    fn strip_comments_preserves_bom() {
        let src = "\u{feff}fn foo() { real(); }\n";
        let out = strip_comments(src);
        assert!(out.starts_with("\u{feff}"), "BOM lost: {out:?}");
        assert!(out.contains("fn foo()"), "signature lost: {out:?}");
    }

    #[test]
    fn strip_comments_preserves_non_doc_attrs() {
        let src = "#[derive(Debug)]\n#[cfg(test)]\n#[inline]\n#[allow(dead_code)]\nfn x() {}\n";
        let out = strip_comments(src);
        assert!(out.contains("#[derive(Debug)]"), "derive lost: {out:?}");
        assert!(out.contains("#[cfg(test)]"), "cfg lost: {out:?}");
        assert!(out.contains("#[inline]"), "inline lost: {out:?}");
        assert!(out.contains("#[allow(dead_code)]"), "allow lost: {out:?}");
    }
}
