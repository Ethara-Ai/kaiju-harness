use proc_macro2::TokenStream;
use quote::quote;
use syn::fold::{self, Fold};
use syn::{
    Block, File, GenericArgument, ImplItemFn, ItemFn, ItemMod, PathArguments, ReturnType,
    TraitItemFn, Type, TypeImplTrait, TypeParamBound,
};

use crate::cfg_test::{has_proc_macro_attr, has_test_attr, has_test_gate};

fn should_preserve_fn(sig: &syn::Signature, attrs: &[syn::Attribute]) -> bool {
    let name = sig.ident.to_string();
    name == "main"
        || sig.constness.is_some()
        || has_test_attr(attrs)
        || has_test_gate(attrs)
        || has_proc_macro_attr(attrs)
}

fn panic_stub_block() -> Box<Block> {
    Box::new(syn::parse_quote!({ panic!("STUB: not implemented"); }))
}

fn extract_impl_trait(ret: &ReturnType) -> Option<&TypeImplTrait> {
    match ret {
        ReturnType::Type(_, ty) => match ty.as_ref() {
            Type::ImplTrait(it) => Some(it),
            _ => None,
        },
        _ => None,
    }
}

fn first_trait_info(impl_trait: &TypeImplTrait) -> Option<(String, &PathArguments)> {
    for bound in &impl_trait.bounds {
        if let TypeParamBound::Trait(tb) = bound {
            let seg = tb.path.segments.last()?;
            return Some((seg.ident.to_string(), &seg.arguments));
        }
    }
    None
}

fn extract_assoc_type(args: &PathArguments, name: &str) -> Option<Type> {
    if let PathArguments::AngleBracketed(ab) = args {
        for arg in &ab.args {
            if let GenericArgument::AssocType(at) = arg {
                if at.ident == name {
                    return Some(at.ty.clone());
                }
            }
        }
    }
    None
}

/// Build a closure stub for `impl Fn(A, B) -> R` / `impl FnMut(...)` / `impl FnOnce(...)`.
///
/// Generates: `|_: A, _: B| -> R { panic!("STUB: not implemented") }`
fn build_fn_trait_closure(args: &PathArguments) -> Option<Box<Block>> {
    if let PathArguments::Parenthesized(paren) = args {
        let params: Vec<TokenStream> = paren
            .inputs
            .iter()
            .enumerate()
            .map(|(i, ty)| {
                let name = syn::Ident::new(&format!("_{}", i), proc_macro2::Span::call_site());
                quote!(#name: #ty)
            })
            .collect();

        let ret_tokens = match &paren.output {
            ReturnType::Default => quote!(),
            ReturnType::Type(arrow, ty) => quote!(#arrow #ty),
        };

        let closure = quote!({
            panic!("STUB: not implemented");
            #[allow(unreachable_code)]
            |#(#params),*| #ret_tokens { panic!("STUB: not implemented") }
        });

        Some(Box::new(syn::parse2(closure).expect("failed to parse Fn closure stub")))
    } else {
        None
    }
}

fn stub_block_for_impl_trait(impl_trait: &TypeImplTrait) -> Box<Block> {
    if let Some((trait_name, args)) = first_trait_info(impl_trait) {
        match trait_name.as_str() {
            "Iterator" => {
                let item_ty = extract_assoc_type(args, "Item")
                    .unwrap_or_else(|| syn::parse_quote!(()));
                return Box::new(syn::parse_quote!({
                    panic!("STUB: not implemented");
                    #[allow(unreachable_code)]
                    std::iter::empty::<#item_ty>()
                }));
            }
            "IntoIterator" => {
                let item_ty = extract_assoc_type(args, "Item")
                    .unwrap_or_else(|| syn::parse_quote!(()));
                return Box::new(syn::parse_quote!({
                    panic!("STUB: not implemented");
                    #[allow(unreachable_code)]
                    Vec::<#item_ty>::new()
                }));
            }
            "Display" | "Debug" => {
                return Box::new(syn::parse_quote!({
                    panic!("STUB: not implemented");
                    #[allow(unreachable_code)]
                    String::new()
                }));
            }
            "AsRef" => {
                return Box::new(syn::parse_quote!({
                    panic!("STUB: not implemented");
                    #[allow(unreachable_code)]
                    String::new()
                }));
            }
            "Clone" | "Copy" => {
                return Box::new(syn::parse_quote!({
                    panic!("STUB: not implemented");
                    #[allow(unreachable_code)]
                    ()
                }));
            }
            "Future" => {
                let output_ty = extract_assoc_type(args, "Output")
                    .unwrap_or_else(|| syn::parse_quote!(()));
                return Box::new(syn::parse_quote!({
                    panic!("STUB: not implemented");
                    #[allow(unreachable_code)]
                    std::future::ready::<#output_ty>(panic!())
                }));
            }
            "Fn" | "FnMut" | "FnOnce" => {
                if let Some(block) = build_fn_trait_closure(args) {
                    return block;
                }
            }
            _ => {}
        }
    }
    Box::new(syn::parse_quote!({
        panic!("STUB: not implemented");
        #[allow(unreachable_code)]
        loop {}
    }))
}

fn make_stub_block(ret: &ReturnType) -> Box<Block> {
    match extract_impl_trait(ret) {
        Some(impl_trait) => stub_block_for_impl_trait(impl_trait),
        None => panic_stub_block(),
    }
}

/// AST folder that replaces function bodies with `panic!("STUB: not implemented")`.
///
/// Preserves:
/// - `fn main()`
/// - Functions with `#[test]` or `#[cfg(test)]` attributes
/// - Trait function declarations (no bodies)
/// - Everything inside `#[cfg(test)]` modules
pub struct StubFolder {
    pub options: StubOptions,
}

/// Configuration for the stubbing transformation.
///
/// `strip_docs` defaults to `true` because the harness contract is to expose
/// only signatures, non-doc attributes, test code, and stub bodies to the
/// agent. Set it to `false` (e.g. via `--keep-docs` on the CLI) only when
/// you explicitly want upstream doc comments and `//` / `/* */` comments
/// preserved in the output.
#[derive(Clone, Copy, Debug)]
pub struct StubOptions {
    /// When `true` (the default), strip every `#[doc = "..."]` attribute (i.e.
    /// `///` outer doc comments, `//!` inner module docs, explicit
    /// `#[doc(...)]`) AND every `//` line comment, `/* */` block comment, and
    /// outer/inner block doc comment from every traversed item. When `false`,
    /// the upstream comments survive verbatim.
    pub strip_docs: bool,
}

impl Default for StubOptions {
    fn default() -> Self {
        Self { strip_docs: true }
    }
}

impl Default for StubFolder {
    fn default() -> Self {
        Self { options: StubOptions::default() }
    }
}

impl StubFolder {
    /// Construct with default options (no doc stripping).
    pub fn new() -> Self { Self::default() }

    /// Construct with explicit options.
    pub fn with_options(options: StubOptions) -> Self { Self { options } }

    /// Helper: if strip_docs is enabled, remove `#[doc = "..."]` from *attrs*.
    fn maybe_strip_doc_attrs(&self, attrs: &mut Vec<syn::Attribute>) {
        if self.options.strip_docs {
            attrs.retain(|a| !is_doc_attr(a));
        }
    }
}

/// Returns true if *attr* is a `#[doc = "..."]` or `#[doc(...)]` attribute.
/// This matches the syntactic form emitted by `///` and `//!` after parsing
/// (both desugar to `#[doc = "..."]` outer/inner attributes).
fn is_doc_attr(attr: &syn::Attribute) -> bool {
    attr.path().is_ident("doc")
}

impl Fold for StubFolder {
    // ===== Items that get their bodies stubbed =====

    fn fold_item_fn(&mut self, mut i: ItemFn) -> ItemFn {
        self.maybe_strip_doc_attrs(&mut i.attrs);
        if should_preserve_fn(&i.sig, &i.attrs) {
            return fold::fold_item_fn(self, i);
        }
        i.block = make_stub_block(&i.sig.output);
        i
    }

    fn fold_impl_item_fn(&mut self, mut i: ImplItemFn) -> ImplItemFn {
        self.maybe_strip_doc_attrs(&mut i.attrs);
        if should_preserve_fn(&i.sig, &i.attrs) {
            return fold::fold_impl_item_fn(self, i);
        }
        i.block = *make_stub_block(&i.sig.output);
        i
    }

    fn fold_trait_item_fn(&mut self, mut i: TraitItemFn) -> TraitItemFn {
        self.maybe_strip_doc_attrs(&mut i.attrs);
        i
    }

    fn fold_item_mod(&mut self, mut i: ItemMod) -> ItemMod {
        if has_test_gate(&i.attrs) {
            return i;
        }
        self.maybe_strip_doc_attrs(&mut i.attrs);
        fold::fold_item_mod(self, i)
    }

    // ===== Types and their members =====

    fn fold_item_struct(&mut self, mut i: syn::ItemStruct) -> syn::ItemStruct {
        self.maybe_strip_doc_attrs(&mut i.attrs);
        fold::fold_item_struct(self, i)
    }
    fn fold_item_enum(&mut self, mut i: syn::ItemEnum) -> syn::ItemEnum {
        self.maybe_strip_doc_attrs(&mut i.attrs);
        fold::fold_item_enum(self, i)
    }
    fn fold_item_union(&mut self, mut i: syn::ItemUnion) -> syn::ItemUnion {
        self.maybe_strip_doc_attrs(&mut i.attrs);
        fold::fold_item_union(self, i)
    }
    fn fold_variant(&mut self, mut v: syn::Variant) -> syn::Variant {
        self.maybe_strip_doc_attrs(&mut v.attrs);
        fold::fold_variant(self, v)
    }
    fn fold_field(&mut self, mut f: syn::Field) -> syn::Field {
        self.maybe_strip_doc_attrs(&mut f.attrs);
        fold::fold_field(self, f)
    }

    // ===== Trait declarations and their members =====

    fn fold_item_trait(&mut self, mut i: syn::ItemTrait) -> syn::ItemTrait {
        self.maybe_strip_doc_attrs(&mut i.attrs);
        fold::fold_item_trait(self, i)
    }
    fn fold_trait_item_type(&mut self, mut i: syn::TraitItemType) -> syn::TraitItemType {
        self.maybe_strip_doc_attrs(&mut i.attrs);
        fold::fold_trait_item_type(self, i)
    }
    fn fold_trait_item_const(&mut self, mut i: syn::TraitItemConst) -> syn::TraitItemConst {
        self.maybe_strip_doc_attrs(&mut i.attrs);
        fold::fold_trait_item_const(self, i)
    }
    fn fold_trait_item_macro(&mut self, mut i: syn::TraitItemMacro) -> syn::TraitItemMacro {
        self.maybe_strip_doc_attrs(&mut i.attrs);
        fold::fold_trait_item_macro(self, i)
    }
    fn fold_item_trait_alias(&mut self, mut i: syn::ItemTraitAlias) -> syn::ItemTraitAlias {
        self.maybe_strip_doc_attrs(&mut i.attrs);
        fold::fold_item_trait_alias(self, i)
    }

    // ===== Impl blocks and their non-fn members =====

    fn fold_item_impl(&mut self, mut i: syn::ItemImpl) -> syn::ItemImpl {
        self.maybe_strip_doc_attrs(&mut i.attrs);
        fold::fold_item_impl(self, i)
    }
    fn fold_impl_item_type(&mut self, mut i: syn::ImplItemType) -> syn::ImplItemType {
        self.maybe_strip_doc_attrs(&mut i.attrs);
        fold::fold_impl_item_type(self, i)
    }
    fn fold_impl_item_const(&mut self, mut i: syn::ImplItemConst) -> syn::ImplItemConst {
        self.maybe_strip_doc_attrs(&mut i.attrs);
        fold::fold_impl_item_const(self, i)
    }
    fn fold_impl_item_macro(&mut self, mut i: syn::ImplItemMacro) -> syn::ImplItemMacro {
        self.maybe_strip_doc_attrs(&mut i.attrs);
        fold::fold_impl_item_macro(self, i)
    }

    // ===== Other top-level items =====

    fn fold_item_const(&mut self, mut i: syn::ItemConst) -> syn::ItemConst {
        self.maybe_strip_doc_attrs(&mut i.attrs);
        fold::fold_item_const(self, i)
    }
    fn fold_item_static(&mut self, mut i: syn::ItemStatic) -> syn::ItemStatic {
        self.maybe_strip_doc_attrs(&mut i.attrs);
        fold::fold_item_static(self, i)
    }
    fn fold_item_type(&mut self, mut i: syn::ItemType) -> syn::ItemType {
        self.maybe_strip_doc_attrs(&mut i.attrs);
        fold::fold_item_type(self, i)
    }
    fn fold_item_use(&mut self, mut i: syn::ItemUse) -> syn::ItemUse {
        self.maybe_strip_doc_attrs(&mut i.attrs);
        fold::fold_item_use(self, i)
    }
    fn fold_item_extern_crate(&mut self, mut i: syn::ItemExternCrate) -> syn::ItemExternCrate {
        self.maybe_strip_doc_attrs(&mut i.attrs);
        fold::fold_item_extern_crate(self, i)
    }
    fn fold_item_macro(&mut self, mut i: syn::ItemMacro) -> syn::ItemMacro {
        self.maybe_strip_doc_attrs(&mut i.attrs);
        fold::fold_item_macro(self, i)
    }

    // ===== extern blocks and their items =====

    fn fold_item_foreign_mod(&mut self, mut i: syn::ItemForeignMod) -> syn::ItemForeignMod {
        self.maybe_strip_doc_attrs(&mut i.attrs);
        fold::fold_item_foreign_mod(self, i)
    }
    fn fold_foreign_item_fn(&mut self, mut i: syn::ForeignItemFn) -> syn::ForeignItemFn {
        self.maybe_strip_doc_attrs(&mut i.attrs);
        fold::fold_foreign_item_fn(self, i)
    }
    fn fold_foreign_item_static(&mut self, mut i: syn::ForeignItemStatic) -> syn::ForeignItemStatic {
        self.maybe_strip_doc_attrs(&mut i.attrs);
        fold::fold_foreign_item_static(self, i)
    }
    fn fold_foreign_item_type(&mut self, mut i: syn::ForeignItemType) -> syn::ForeignItemType {
        self.maybe_strip_doc_attrs(&mut i.attrs);
        fold::fold_foreign_item_type(self, i)
    }
    fn fold_foreign_item_macro(&mut self, mut i: syn::ForeignItemMacro) -> syn::ForeignItemMacro {
        self.maybe_strip_doc_attrs(&mut i.attrs);
        fold::fold_foreign_item_macro(self, i)
    }
}

/// Stub a parsed file with default options (no doc stripping). Backward
/// compatible with the original signature.
pub fn stub_file(file: File) -> File {
    stub_file_with_options(file, StubOptions::default())
}

/// Stub a parsed file with explicit options.
///
/// Routes through `stub_source_with_options` (byte-span surgery) by
/// re-emitting → stubbing → re-parsing, so the public in-memory API has
/// identical semantics to the source-string API. The old `Fold`-based
/// pipeline is retained as `StubFolder` for direct callers that explicitly
/// want it, but it cannot stub items inside macro invocations and does not
/// stub trait-method default bodies (B3/B6 from the review).
pub fn stub_file_with_options(file: File, options: StubOptions) -> File {
    let source = prettyplease::unparse(&file);
    match stub_source_with_options(&source, options) {
        Ok(stubbed) => syn::parse_file(&stubbed).unwrap_or(file),
        Err(_) => file,
    }
}

/// Returns `Err` if `syn::parse_file` fails. Uses default options.
pub fn stub_source(source: &str) -> Result<String, String> {
    stub_source_with_options(source, StubOptions::default())
}

/// Returns `Err` if `syn::parse_file` fails. Uses explicit options.
///
/// Pipeline: parse → byte-span surgery (collect fn-body edits + optional
/// doc-attr edits) → splice into ORIGINAL source. Everything outside fn-body
/// byte ranges is byte-identical to the input. Comments, blank lines, macro
/// invocations, and doc comments inside macros are all preserved verbatim.
pub fn stub_source_with_options(source: &str, options: StubOptions) -> Result<String, String> {
    let parsed = syn::parse_file(source).map_err(|e| format!("syn parse error: {e}"))?;
    let report = crate::span_edit::collect_edits_with_report(
        &parsed,
        crate::span_edit::CollectOptions { strip_docs: options.strip_docs },
        source.len(),
    );
    if !report.unknown_macros_skipped.is_empty() {
        let names: Vec<_> = report.unknown_macros_skipped.iter().take(8).cloned().collect();
        eprintln!(
            "ruststubber: warning: could not recurse into {} unknown macro invocation(s) — bodies inside MAY leak: {}{}",
            report.unknown_macros_skipped.len(),
            names.join(", "),
            if report.unknown_macros_skipped.len() > 8 { ", ..." } else { "" }
        );
    }
    let stubbed = crate::span_edit::apply_edits(source, report.edits);
    if options.strip_docs {
        Ok(crate::span_edit::strip_comments(&stubbed))
    } else {
        Ok(stubbed)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_normal_fn_is_stubbed() {
        let src = r#"
fn add(a: i32, b: i32) -> i32 {
    a + b
}
"#;
        let out = stub_source(src).unwrap();
        assert!(out.contains(r#"panic!("STUB: not implemented")"#), "got: {out}");
        assert!(!out.contains("a + b"), "got: {out}");
    }

    #[test]
    fn test_test_fn_is_preserved() {
        let src = r#"
#[test]
fn test_something() {
    assert_eq!(1, 1);
}
"#;
        let out = stub_source(src).unwrap();
        assert!(out.contains("assert_eq!"));
        assert!(!out.contains(r#"panic!("STUB: not implemented")"#), "test fn body should not be stubbed: {out}");
    }

    #[test]
    fn test_main_fn_is_preserved() {
        let src = r#"
fn main() {
    println!("hello");
}
"#;
        let out = stub_source(src).unwrap();
        assert!(out.contains("println!"));
    }

    #[test]
    fn test_cfg_test_module_preserved() {
        let src = r#"
#[cfg(test)]
mod tests {
    fn helper() -> i32 { 42 }

    #[test]
    fn it_works() {
        assert_eq!(helper(), 42);
    }
}
"#;
        let out = stub_source(src).unwrap();
        assert!(out.contains("assert_eq!"));
        assert!(out.contains("42"));
    }

    #[test]
    fn test_impl_method_is_stubbed() {
        let src = r#"
struct Foo;
impl Foo {
    fn bar(&self) -> i32 { 42 }
}
"#;
        let out = stub_source(src).unwrap();
        assert!(out.contains(r#"panic!("STUB: not implemented")"#), "got: {out}");
        assert!(!out.contains("42"), "got: {out}");
    }

    #[test]
    fn test_trait_decl_unchanged() {
        let src = r#"
trait MyTrait {
    fn do_thing(&self) -> bool;
}
"#;
        let out = stub_source(src).unwrap();
        assert!(out.contains("fn do_thing"));
    }

    #[test]
    fn test_impl_iterator_return_left_unstubbed() {
        // RPIT can't be stubbed with a divergent body (opaque type infers to `()`
        // → doesn't compile). Real body must be retained so the base builds.
        let src = r#"
fn get_items() -> impl Iterator<Item = i32> {
    vec![1, 2, 3].into_iter()
}
"#;
        let out = stub_source(src).unwrap();
        assert!(out.contains("vec![1, 2, 3].into_iter()"), "real body must be retained: {out}");
        assert!(!out.contains("panic!"), "must NOT emit a non-compiling stub: {out}");
    }

    #[test]
    fn test_impl_iterator_with_lifetime_left_unstubbed() {
        let src = r#"
fn get_strs<'a>(v: &'a [String]) -> impl Iterator<Item = &'a str> + 'a {
    v.iter().map(|s| s.as_str())
}
"#;
        let out = stub_source(src).unwrap();
        assert!(out.contains("v.iter().map"), "real body must be retained: {out}");
        assert!(!out.contains("panic!"), "must NOT emit a non-compiling stub: {out}");
    }

    #[test]
    fn test_impl_display_return_left_unstubbed() {
        let src = r#"
fn display_thing() -> impl std::fmt::Display {
    "hello"
}
"#;
        let out = stub_source(src).unwrap();
        assert!(out.contains(r#""hello""#), "real body must be retained: {out}");
        assert!(!out.contains("panic!"), "must NOT emit a non-compiling stub: {out}");
    }

    #[test]
    fn test_impl_into_iterator_return_left_unstubbed() {
        let src = r#"
fn get_collection() -> impl IntoIterator<Item = u8> {
    vec![1u8, 2, 3]
}
"#;
        let out = stub_source(src).unwrap();
        assert!(out.contains("vec![1u8, 2, 3]"), "real body must be retained: {out}");
        assert!(!out.contains("panic!"), "must NOT emit a non-compiling stub: {out}");
    }

    #[test]
    fn test_unknown_impl_trait_left_unstubbed() {
        let src = r#"
trait Custom {}
fn get_custom() -> impl Custom {
    struct X;
    impl Custom for X {}
    X
}
"#;
        let out = stub_source(src).unwrap();
        assert!(out.contains("impl Custom for X"), "real body must be retained: {out}");
        assert!(!out.contains("panic!"), "must NOT emit a non-compiling stub: {out}");
    }

    #[test]
    fn test_concrete_return_uses_loop() {
        let src = r#"
fn get_vec() -> Vec<i32> {
    vec![1, 2, 3]
}
"#;
        let out = stub_source(src).unwrap();
        assert!(out.contains(r#"{ panic!("STUB: not implemented") }"#), "got: {out}");
        assert!(!out.contains("std::iter::empty"), "got: {out}");
    }

    #[test]
    fn test_impl_method_with_impl_trait_return_left_unstubbed() {
        let src = r#"
struct Foo;
impl Foo {
    fn items(&self) -> impl Iterator<Item = String> {
        vec!["a".to_string()].into_iter()
    }
}
"#;
        let out = stub_source(src).unwrap();
        assert!(out.contains(r#"vec!["a".to_string()].into_iter()"#), "real body must be retained: {out}");
        assert!(!out.contains("panic!"), "must NOT emit a non-compiling stub: {out}");
    }

    #[test]
    fn test_impl_fn_mut_return_left_unstubbed() {
        let src = r#"
use std::cmp::Ordering;
struct GridItem;
fn cmp_items(_axis: u32) -> impl FnMut(&GridItem, &GridItem) -> Ordering {
    move |_a, _b| Ordering::Equal
}
"#;
        let out = stub_source(src).unwrap();
        assert!(out.contains("Ordering::Equal"), "real body must be retained: {out}");
        assert!(!out.contains("panic!"), "must NOT emit a non-compiling stub: {out}");
    }

    #[test]
    fn test_impl_fn_once_no_return_left_unstubbed() {
        let src = r#"
fn make_callback() -> impl FnOnce(i32) {
    |x| println!("{}", x)
}
"#;
        let out = stub_source(src).unwrap();
        assert!(out.contains("println"), "real body must be retained: {out}");
        assert!(!out.contains("panic!"), "must NOT emit a non-compiling stub: {out}");
    }

    #[test]
    fn test_const_fn_is_preserved() {
        let src = r#"
struct Styles { header: u8 }
impl Styles {
    pub const fn styled() -> Self {
        Self { header: 42 }
    }
    pub const fn header(mut self, val: u8) -> Self {
        self.header = val;
        self
    }
}
"#;
        let out = stub_source(src).unwrap();
        assert!(out.contains("42"), "got: {out}");
        assert!(!out.contains(r#"panic!("STUB: not implemented")"#), "const fn body should not be stubbed: {out}");
    }

    // ===== strip_docs option tests (universal coverage across item kinds) =====

    fn stub_strip_docs(src: &str) -> String {
        stub_source_with_options(src, StubOptions { strip_docs: true }).unwrap()
    }

    #[test]
    fn strip_docs_removes_outer_doc_on_fn() {
        let src = r#"
/// This is a documented function that returns 42.
fn answer() -> i32 { 42 }
"#;
        let out = stub_strip_docs(src);
        assert!(!out.contains("documented function"), "doc should be stripped: {out}");
        assert!(out.contains("fn answer"), "signature kept: {out}");
        assert!(out.contains(r#"panic!("STUB: not implemented")"#), "got: {out}");
    }

    #[test]
    fn strip_docs_removes_inner_module_docs() {
        let src = r#"
//! Top-of-file module documentation that should be stripped.
//! With multiple lines.
fn foo() {}
"#;
        let out = stub_strip_docs(src);
        assert!(!out.contains("module documentation"), "file-level //! should be stripped: {out}");
        assert!(!out.contains("multiple lines"));
    }

    #[test]
    fn strip_docs_removes_struct_and_field_docs() {
        let src = r#"
/// A documented struct.
pub struct Foo {
    /// A documented field.
    pub bar: i32,
}
"#;
        let out = stub_strip_docs(src);
        assert!(!out.contains("documented struct"));
        assert!(!out.contains("documented field"));
        assert!(out.contains("struct Foo"));
        assert!(out.contains("bar"));
    }

    #[test]
    fn strip_docs_removes_enum_and_variant_docs() {
        let src = r#"
/// A documented enum.
pub enum Color {
    /// The red variant.
    Red,
    /// The green variant.
    Green,
}
"#;
        let out = stub_strip_docs(src);
        assert!(!out.contains("documented enum"));
        assert!(!out.contains("red variant"));
        assert!(!out.contains("green variant"));
        assert!(out.contains("enum Color"));
    }

    #[test]
    fn strip_docs_removes_trait_and_trait_item_docs() {
        let src = r#"
/// A documented trait.
pub trait Speak {
    /// A documented method.
    fn hello(&self) -> String;
    /// A documented type.
    type Output;
    /// A documented constant.
    const NAME: &'static str;
}
"#;
        let out = stub_strip_docs(src);
        assert!(!out.contains("documented trait"));
        assert!(!out.contains("documented method"));
        assert!(!out.contains("documented type"));
        assert!(!out.contains("documented constant"));
        assert!(out.contains("trait Speak"));
        assert!(out.contains("fn hello"));
    }

    #[test]
    fn strip_docs_removes_impl_item_docs() {
        let src = r#"
struct Foo;
/// Impl block doc.
impl Foo {
    /// Documented method.
    pub fn bar(&self) -> i32 { 42 }
    /// Documented assoc type.
    type Inner = i32;
    /// Documented assoc const.
    const X: i32 = 1;
}
"#;
        let out = stub_strip_docs(src);
        assert!(!out.contains("Impl block doc"));
        assert!(!out.contains("Documented method"));
        assert!(!out.contains("Documented assoc type"));
        assert!(!out.contains("Documented assoc const"));
        assert!(out.contains("fn bar"));
    }

    #[test]
    fn strip_docs_removes_top_level_const_static_type_docs() {
        let src = r#"
/// A documented const.
pub const FOO: i32 = 1;
/// A documented static.
pub static BAR: i32 = 2;
/// A documented type alias.
pub type Id = u64;
"#;
        let out = stub_strip_docs(src);
        assert!(!out.contains("documented const"));
        assert!(!out.contains("documented static"));
        assert!(!out.contains("documented type alias"));
    }

    #[test]
    fn strip_docs_handles_explicit_doc_attr_form() {
        // #[doc = "..."] is the explicit form (what /// desugars to). Must strip both.
        let src = r#"
#[doc = "Explicit outer doc form."]
pub fn foo() -> i32 { 0 }
"#;
        let out = stub_strip_docs(src);
        assert!(!out.contains("Explicit outer doc form"));
        assert!(out.contains("fn foo"));
    }

    #[test]
    fn strip_docs_is_now_the_default() {
        let src = r#"
/// Should be stripped by default now.
fn drop_me() -> i32 { 1 }
"#;
        let out = stub_source(src).unwrap();
        assert!(!out.contains("Should be stripped"), "doc should be stripped by default: {out}");
        assert!(out.contains("fn drop_me"), "signature preserved: {out}");
    }

    #[test]
    fn opt_out_keep_docs_preserves_docs() {
        let src = r#"
/// Should remain when strip_docs=false.
fn keep_me() -> i32 { 1 }
"#;
        let out = stub_source_with_options(src, StubOptions { strip_docs: false }).unwrap();
        assert!(out.contains("Should remain"), "doc should be preserved when strip_docs=false: {out}");
    }

    #[test]
    fn strip_docs_strips_extern_block_items() {
        let src = r#"
/// Doc on extern block.
extern "C" {
    /// Doc on extern fn.
    pub fn libc_func() -> i32;
    /// Doc on extern static.
    pub static LIBC_STATIC: i32;
}
"#;
        let out = stub_strip_docs(src);
        assert!(!out.contains("Doc on extern"));
        assert!(out.contains("libc_func") || out.contains("LIBC_STATIC"));
    }

}
