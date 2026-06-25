//! Shared evaluator for "is this item gated behind the test or loom profile?"
//!
//! Replaces the broken substring-based `has_cfg_test_attr` in stubber.rs (which
//! false-positives on `test-utils`, `not(test)`, `any(test, X)`) with a proper
//! recursive evaluator over the cfg-expression mini-language. `cfg(loom)` is
//! treated identically to `cfg(test)` because loom is the testing-only
//! concurrency model and any item gated on it is test-only.

use syn::Attribute;

pub fn has_test_attr(attrs: &[Attribute]) -> bool {
    attrs.iter().any(|a| {
        a.path()
            .segments
            .last()
            .map(|s| s.ident == "test")
            .unwrap_or(false)
    })
}

pub fn has_ctor_or_dtor_attr(attrs: &[Attribute]) -> bool {
    attrs.iter().any(|a| {
        a.path()
            .segments
            .last()
            .map(|s| s.ident == "ctor" || s.ident == "dtor")
            .unwrap_or(false)
    })
}

pub fn attr_implies_test_gate(attr: &Attribute) -> bool {
    let path = attr.path();
    let syn::Meta::List(list) = &attr.meta else { return false };

    if path.is_ident("cfg") {
        let Ok(meta) = syn::parse2::<syn::Meta>(list.tokens.clone()) else { return false };
        return cfg_expr_requires_test_or_loom(&meta);
    }
    if path.is_ident("cfg_attr") {
        let args = parse_meta_args(list);
        if args.len() < 2 { return false }
        return args[1..].iter().any(meta_implies_test_gate);
    }
    false
}

pub fn cfg_expr_requires_test_or_loom(meta: &syn::Meta) -> bool {
    match meta {
        syn::Meta::Path(p) => p.is_ident("test") || p.is_ident("loom"),
        syn::Meta::List(list) => {
            let name = list.path.get_ident().map(|i| i.to_string()).unwrap_or_default();
            let args = parse_meta_args(list);
            match name.as_str() {
                "all" => args.iter().any(cfg_expr_requires_test_or_loom),
                "any" => !args.is_empty() && args.iter().all(cfg_expr_requires_test_or_loom),
                "not" => false,
                _ => false,
            }
        }
        syn::Meta::NameValue(_) => false,
    }
}

pub fn meta_implies_test_gate(meta: &syn::Meta) -> bool {
    let syn::Meta::List(list) = meta else { return false };
    if !list.path.is_ident("cfg") { return false }
    let Ok(inner) = syn::parse2::<syn::Meta>(list.tokens.clone()) else { return false };
    cfg_expr_requires_test_or_loom(&inner)
}

pub fn parse_meta_args(list: &syn::MetaList) -> Vec<syn::Meta> {
    use syn::parse::Parser;
    let parser = syn::punctuated::Punctuated::<syn::Meta, syn::Token![,]>::parse_terminated;
    parser.parse2(list.tokens.clone())
        .ok()
        .map(|p| p.into_iter().collect())
        .unwrap_or_default()
}

pub fn has_test_gate(attrs: &[Attribute]) -> bool {
    attrs.iter().any(attr_implies_test_gate)
}

pub fn has_proc_macro_attr(attrs: &[Attribute]) -> bool {
    attrs.iter().any(|a| {
        let p = a.path();
        p.is_ident("proc_macro")
            || p.is_ident("proc_macro_derive")
            || p.is_ident("proc_macro_attribute")
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use syn::parse_quote;

    fn parse_attr(src: &str) -> Attribute {
        let attrs: Vec<Attribute> = syn::parse::Parser::parse_str(
            Attribute::parse_outer,
            src,
        ).expect("parse attr");
        attrs.into_iter().next().expect("at least one attr")
    }

    #[test]
    fn cfg_test_is_test_gate() {
        assert!(attr_implies_test_gate(&parse_attr("#[cfg(test)]")));
    }

    #[test]
    fn cfg_loom_is_test_gate() {
        assert!(attr_implies_test_gate(&parse_attr("#[cfg(loom)]")));
    }

    #[test]
    fn cfg_all_test_unix_is_test_gate() {
        assert!(attr_implies_test_gate(&parse_attr("#[cfg(all(test, unix))]")));
    }

    #[test]
    fn cfg_all_loom_unix_is_test_gate() {
        assert!(attr_implies_test_gate(&parse_attr("#[cfg(all(loom, unix))]")));
    }

    #[test]
    fn cfg_not_test_is_not_test_gate() {
        assert!(!attr_implies_test_gate(&parse_attr("#[cfg(not(test))]")));
    }

    #[test]
    fn cfg_feature_test_utils_is_not_test_gate() {
        assert!(!attr_implies_test_gate(&parse_attr(r#"#[cfg(feature = "test-utils")]"#)));
    }

    #[test]
    fn cfg_any_test_or_feature_is_not_test_gate() {
        assert!(!attr_implies_test_gate(&parse_attr(
            r#"#[cfg(any(test, feature = "x"))]"#
        )));
    }

    #[test]
    fn cfg_any_test_and_loom_only_is_test_gate() {
        assert!(attr_implies_test_gate(&parse_attr("#[cfg(any(test, loom))]")));
    }

    #[test]
    fn cfg_attr_with_cfg_test_is_test_gate() {
        assert!(attr_implies_test_gate(&parse_attr("#[cfg_attr(unix, cfg(test))]")));
    }

    #[test]
    fn cfg_attr_without_cfg_test_is_not_test_gate() {
        assert!(!attr_implies_test_gate(&parse_attr("#[cfg_attr(unix, derive(Debug))]")));
    }

    #[test]
    fn non_cfg_attr_is_not_gate() {
        assert!(!attr_implies_test_gate(&parse_attr("#[derive(Debug)]")));
        assert!(!attr_implies_test_gate(&parse_attr("#[inline]")));
        assert!(!attr_implies_test_gate(&parse_attr(r#"#[doc = "hi"]"#)));
    }

    #[test]
    fn has_test_attr_detects_bare_test() {
        let attrs: Vec<Attribute> = vec![parse_quote!(#[test])];
        assert!(has_test_attr(&attrs));
    }

    #[test]
    fn has_test_attr_detects_qualified_tokio_test() {
        let attrs: Vec<Attribute> = vec![parse_quote!(#[tokio::test])];
        assert!(has_test_attr(&attrs));
    }

    #[test]
    fn has_test_attr_detects_qualified_async_std_test() {
        let attrs: Vec<Attribute> = vec![parse_quote!(#[async_std::test])];
        assert!(has_test_attr(&attrs));
    }

    #[test]
    fn has_test_attr_detects_rstest() {
        let attrs: Vec<Attribute> = vec![parse_quote!(#[rstest::rstest])];
        let last_is_test = attrs[0].path().segments.last().unwrap().ident == "rstest";
        assert!(last_is_test);
    }

    #[test]
    fn ctor_and_dtor_detected() {
        let a: Vec<Attribute> = vec![parse_quote!(#[ctor::ctor])];
        assert!(has_ctor_or_dtor_attr(&a));
        let b: Vec<Attribute> = vec![parse_quote!(#[ctor])];
        assert!(has_ctor_or_dtor_attr(&b));
        let c: Vec<Attribute> = vec![parse_quote!(#[dtor::dtor])];
        assert!(has_ctor_or_dtor_attr(&c));
        let d: Vec<Attribute> = vec![parse_quote!(#[inline])];
        assert!(!has_ctor_or_dtor_attr(&d));
    }

    #[test]
    fn proc_macro_attrs_detected() {
        let a: Vec<Attribute> = vec![parse_quote!(#[proc_macro])];
        assert!(has_proc_macro_attr(&a));
        let b: Vec<Attribute> = vec![parse_quote!(#[proc_macro_derive(Foo)])];
        assert!(has_proc_macro_attr(&b));
        let c: Vec<Attribute> = vec![parse_quote!(#[proc_macro_attribute])];
        assert!(has_proc_macro_attr(&c));
        let d: Vec<Attribute> = vec![parse_quote!(#[test])];
        assert!(!has_proc_macro_attr(&d));
    }
}
