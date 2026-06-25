//! Re-parse macro invocation bodies as `Vec<syn::Item>` so the byte-span
//! surgery pipeline can descend into items wrapped in `cfg_*!`, `feature!`,
//! `pin_project!`, etc.
//!
//! Critical property (empirically verified): `proc_macro2::Span::byte_range()`
//! on AST nodes produced via `syn::parse2(mac.tokens.clone())` returns offsets
//! into the ORIGINAL source string, NOT into a synthesized token buffer. This
//! is what makes byte-span surgery valid across macro boundaries.

use proc_macro2::{Delimiter, TokenStream, TokenTree};
use syn::{Item, Macro};

const KNOWN_ITEMS_SHAPED: &[&str] = &[
    "cfg_rt", "cfg_not_rt",
    "cfg_io", "cfg_io_util", "cfg_io_driver",
    "cfg_fs",
    "cfg_net",
    "cfg_signal", "cfg_sync", "cfg_time",
    "cfg_process", "cfg_trace",
    "cfg_not_wasi", "cfg_not_wasip1", "cfg_wasi",
    "cfg_windows", "cfg_unix",
    "cfg_loom", "cfg_not_loom",
    "feature",
    "pin_project", "pin_project_lite",
    "cfg_codec", "cfg_compat",
];

pub fn parse_macro_items(mac: &Macro) -> Option<Vec<Item>> {
    let name = mac.path.get_ident().map(|i| i.to_string()).unwrap_or_default();

    if name == "cfg_if" {
        return parse_cfg_if_branches(&mac.tokens);
    }

    if KNOWN_ITEMS_SHAPED.contains(&name.as_str()) {
        return parse_as_items(&mac.tokens);
    }

    parse_as_items(&mac.tokens)
}

fn parse_as_items(tokens: &TokenStream) -> Option<Vec<Item>> {
    if let Ok(file) = syn::parse2::<syn::File>(tokens.clone()) {
        return Some(file.items);
    }
    if let Ok(item) = syn::parse2::<syn::Item>(tokens.clone()) {
        return Some(vec![item]);
    }
    None
}

fn parse_cfg_if_branches(tokens: &TokenStream) -> Option<Vec<Item>> {
    let mut items: Vec<Item> = Vec::new();
    let mut found_any = false;
    for tt in tokens.clone().into_iter() {
        if let TokenTree::Group(g) = tt {
            if g.delimiter() == Delimiter::Brace {
                if let Some(branch_items) = parse_as_items(&g.stream()) {
                    items.extend(branch_items);
                    found_any = true;
                }
            }
        }
    }
    if found_any { Some(items) } else { None }
}

pub fn is_macro_definition(item_macro: &syn::ItemMacro) -> bool {
    item_macro.ident.is_some()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse_first_mac(src: &str) -> Macro {
        let file: syn::File = syn::parse_str(src).expect("parse_str");
        match &file.items[0] {
            Item::Macro(m) => m.mac.clone(),
            _ => panic!("first item not a macro invocation"),
        }
    }

    #[test]
    fn cfg_rt_with_fn_yields_one_item() {
        let src = "cfg_rt! { pub fn spawn() { real() } }";
        let mac = parse_first_mac(src);
        let items = parse_macro_items(&mac).expect("should parse");
        assert_eq!(items.len(), 1);
        assert!(matches!(items[0], Item::Fn(_)));
    }

    #[test]
    fn cfg_rt_with_multiple_items() {
        let src = "cfg_rt! { pub fn a() {} pub fn b() {} }";
        let mac = parse_first_mac(src);
        let items = parse_macro_items(&mac).expect("should parse");
        assert_eq!(items.len(), 2);
    }

    #[test]
    fn cfg_not_rt_with_struct_and_impl() {
        let src = "cfg_not_rt! { struct S; impl S { fn x(&self) {} } }";
        let mac = parse_first_mac(src);
        let items = parse_macro_items(&mac).expect("should parse");
        assert_eq!(items.len(), 2);
        assert!(matches!(items[0], Item::Struct(_)));
        assert!(matches!(items[1], Item::Impl(_)));
    }

    #[test]
    fn feature_with_inner_attr_yields_items() {
        let src = "feature! { #![unix] pub fn z() { x() } }";
        let mac = parse_first_mac(src);
        let items = parse_macro_items(&mac).expect("should parse");
        assert_eq!(items.len(), 1);
    }

    #[test]
    fn pin_project_with_struct_yields_one_item() {
        let src = "pin_project! { pub struct S { #[pin] x: T } }";
        let mac = parse_first_mac(src);
        let items = parse_macro_items(&mac).expect("should parse");
        assert_eq!(items.len(), 1);
        assert!(matches!(items[0], Item::Struct(_)));
    }

    #[test]
    fn cfg_if_extracts_items_from_both_branches() {
        let src = "cfg_if! { if #[cfg(unix)] { fn u() {} } else { fn w() {} } }";
        let mac = parse_first_mac(src);
        let items = parse_macro_items(&mac).expect("cfg_if! should yield items");
        assert_eq!(items.len(), 2);
        for it in &items {
            assert!(matches!(it, Item::Fn(_)));
        }
    }

    #[test]
    fn cfg_if_three_branches_extracted() {
        let src = "cfg_if! { if #[cfg(unix)] { fn a() {} } else if #[cfg(windows)] { fn b() {} } else { fn c() {} } }";
        let mac = parse_first_mac(src);
        let items = parse_macro_items(&mac).expect("3-branch cfg_if!");
        assert_eq!(items.len(), 3);
    }

    #[test]
    fn unknown_macro_with_item_shape_parses() {
        let src = "my_custom_macro! { fn foo() { real() } }";
        let mac = parse_first_mac(src);
        let items = parse_macro_items(&mac).expect("fallback should parse items");
        assert_eq!(items.len(), 1);
    }

    #[test]
    fn macro_with_non_item_tokens_returns_none() {
        let src = r#"println! { "hello {}", 42 }"#;
        let mac = parse_first_mac(src);
        assert!(parse_macro_items(&mac).is_none());
    }

    #[test]
    fn macro_definition_detected() {
        let src = "macro_rules! foo { () => {} }";
        let file: syn::File = syn::parse_str(src).expect("parse");
        let item_macro = match &file.items[0] {
            Item::Macro(m) => m.clone(),
            _ => unreachable!(),
        };
        assert!(is_macro_definition(&item_macro));
    }

    #[test]
    fn macro_invocation_not_definition() {
        let src = "cfg_rt! { fn x() {} }";
        let file: syn::File = syn::parse_str(src).expect("parse");
        let item_macro = match &file.items[0] {
            Item::Macro(m) => m.clone(),
            _ => unreachable!(),
        };
        assert!(!is_macro_definition(&item_macro));
    }

    #[test]
    fn nested_cfg_rt_cfg_fs_both_parse() {
        let src = "cfg_rt! { cfg_fs! { fn nested() { real() } } }";
        let outer = parse_first_mac(src);
        let outer_items = parse_macro_items(&outer).expect("outer parses");
        assert_eq!(outer_items.len(), 1);
        let inner_mac = match &outer_items[0] {
            Item::Macro(m) => m.mac.clone(),
            _ => panic!("inner not a macro"),
        };
        let inner_items = parse_macro_items(&inner_mac).expect("inner parses");
        assert_eq!(inner_items.len(), 1);
        assert!(matches!(inner_items[0], Item::Fn(_)));
    }
}
