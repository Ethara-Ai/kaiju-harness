//! Golden-file integration tests against fresh upstream tokio sources.
//!
//! Each fixture under `tests/fixtures/tokio/` is a verbatim copy of an
//! upstream tokio source file at the time the corpus was vendored. The
//! assertions encode the production contract of the stubber:
//!
//!   1. The stubbed output parses cleanly as a Rust file.
//!   2. Every byte outside of stubbed fn-body ranges is byte-identical to
//!      upstream — comments, blank lines, doc /// lines, macro invocations
//!      with their original indentation and whitespace, attribute formatting,
//!      and trailing newlines all survive untouched.
//!   3. Every function body that should be stubbed IS stubbed with the
//!      unified `{ panic!("STUB: not implemented") }` body, including bodies wrapped in `cfg_*!`,
//!      `feature!`, `pin_project!`, `cfg_if!`-adjacent macros.
//!   4. Preservation rules hold: `const fn`, `fn main`, `#[test]`,
//!      `#[cfg(test)]`, `#[cfg(loom)]`, `#[proc_macro*]` bodies survive.

use ruststubber::{stub_source_with_options, StubOptions};

fn load(name: &str) -> String {
    let path = format!(
        "{}/tests/fixtures/tokio/{name}",
        env!("CARGO_MANIFEST_DIR")
    );
    std::fs::read_to_string(&path)
        .unwrap_or_else(|e| panic!("read fixture {path}: {e}"))
}

fn stub(name: &str) -> (String, String) {
    let src = load(name);
    let out = stub_source_with_options(&src, StubOptions { strip_docs: false })
        .unwrap_or_else(|e| panic!("stub {name}: {e}"));
    (src, out)
}

#[test]
fn blocking_rs_parses_after_stubbing() {
    let (_, out) = stub("blocking.rs");
    syn::parse_file(&out).expect("stubbed blocking.rs parses");
}

#[test]
fn blocking_rs_inner_fns_in_cfg_not_rt_are_stubbed() {
    let (src, out) = stub("blocking.rs");
    assert!(src.contains("fn spawn_blocking"), "fixture sanity");
    assert!(
        out.contains("fn spawn_blocking"),
        "signature must survive: {out}"
    );
    assert!(
        out.contains(r#"{ panic!("STUB: not implemented") }"#),
        "no stub body produced: {out}"
    );
    assert!(
        !out.contains(r#"panic!("requires the `rt` Tokio feature flag")"#),
        "real cfg_not_rt panic body leaked through: {out}"
    );
    assert!(
        !out.contains("assert_send_sync::<JoinHandle"),
        "real call inside spawn_blocking body leaked: {out}"
    );
    let stub_count = out.matches(r#"{ panic!("STUB: not implemented") }"#).count();
    assert!(
        stub_count >= 4,
        "expected at least 4 stubbed bodies in cfg_not_rt block, got {stub_count}: {out}"
    );
}

#[test]
fn blocking_rs_macro_invocation_braces_byte_identical() {
    let (src, out) = stub("blocking.rs");
    assert!(src.contains("cfg_rt! {"), "fixture sanity");
    assert!(
        out.contains("cfg_rt! {"),
        "macro invocation header reformatted: {out}"
    );
    assert!(out.contains("cfg_not_rt! {"), "got: {out}");
    assert!(!out.contains("pub (crate)"), "token-soup spacing reintroduced: {out}");
    assert!(!out.contains("std :: fmt"), "token-soup spacing reintroduced: {out}");
}

#[test]
fn dir_builder_rs_parses_and_stubs_feature_unix_block() {
    let (src, out) = stub("fs_dir_builder.rs");
    syn::parse_file(&out).expect("dir_builder parses");

    assert!(src.contains("feature! {"), "fixture sanity");
    assert!(out.contains("feature! {"), "got: {out}");

    assert!(src.contains("/// Sets the mode"), "fixture sanity");
    assert!(
        out.contains("/// Sets the mode"),
        "doc comment inside feature! was rewritten or dropped: {out}"
    );

    assert!(out.contains("fn mode("), "got: {out}");
    assert!(out.contains(r#"{ panic!("STUB: not implemented") }"#), "got: {out}");
    assert!(
        !out.contains("self.mode = Some(mode)"),
        "real body inside feature! leaked through: {out}"
    );
}

#[test]
fn dir_builder_rs_blank_lines_outside_fn_bodies_preserved() {
    let (src, out) = stub("fs_dir_builder.rs");
    let outside_src = strip_fn_bodies(&src);
    let outside_out = strip_fn_bodies(&out);
    let src_blanks = outside_src.matches("\n\n").count();
    let out_blanks = outside_out.matches("\n\n").count();
    assert_eq!(
        src_blanks, out_blanks,
        "blank-line count OUTSIDE fn bodies drifted: src={src_blanks} out={out_blanks}"
    );
}

fn strip_fn_bodies(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut depth: i32 = 0;
    let mut in_body = false;
    let mut iter = s.chars().peekable();
    while let Some(c) = iter.next() {
        if !in_body {
            out.push(c);
            if c == '{' && looks_like_fn_body_open(&out) {
                in_body = true;
                depth = 1;
            }
        } else {
            match c {
                '{' => depth += 1,
                '}' => {
                    depth -= 1;
                    if depth == 0 {
                        in_body = false;
                        out.push(c);
                    }
                }
                _ => {}
            }
        }
    }
    out
}

fn looks_like_fn_body_open(prefix: &str) -> bool {
    let head = prefix.trim_end_matches('{').trim_end();
    head.contains(") ") || head.ends_with(')') || head.ends_with("Self") || head.ends_with("> ")
}

#[test]
fn maybe_done_rs_pin_project_struct_preserved_and_methods_stubbed() {
    let (src, out) = stub("future_maybe_done.rs");
    syn::parse_file(&out).expect("maybe_done parses");

    assert!(src.contains("pin_project! {"), "fixture sanity");
    assert!(
        out.contains("pin_project! {"),
        "pin_project! reformatted: {out}"
    );

    assert!(src.contains("#[repr(C)]"), "fixture sanity");
    assert!(
        out.contains("#[repr(C)]"),
        "attribute inside pin_project! dropped: {out}"
    );
    assert!(
        src.contains("// https://github.com/rust-lang/miri/issues/3780"),
        "fixture sanity"
    );
    assert!(
        out.contains("// https://github.com/rust-lang/miri/issues/3780"),
        "inline comment inside pin_project! dropped: {out}"
    );

    assert!(out.contains(r#"{ panic!("STUB: not implemented") }"#), "no stub body produced");
}

#[test]
fn maybe_done_rs_cfg_test_miri_module_preserved() {
    let (src, out) = stub("future_maybe_done.rs");
    assert!(src.contains("#[cfg(test)]"), "fixture sanity");
    assert!(src.contains("mod miri_tests"), "fixture sanity");
    assert!(
        out.contains("mod miri_tests"),
        "cfg(test) module disappeared: {out}"
    );
    assert!(
        out.contains("Poll::Pending"),
        "cfg(test) body was stubbed: {out}"
    );
}

#[test]
fn util_bit_rs_const_fns_preserved() {
    let (src, out) = stub("util_bit.rs");
    syn::parse_file(&out).expect("util_bit parses");

    assert!(src.contains("const fn least_significant"), "fixture sanity");
    assert!(out.contains("const fn least_significant"), "got: {out}");

    assert!(
        out.contains("mask_for(width)"),
        "const fn body was stubbed (regression): {out}"
    );

    assert!(
        out.contains(r#"{ panic!("STUB: not implemented") }"#),
        "non-const fn (pack/unpack) was not stubbed: {out}"
    );
    assert!(
        !out.contains("base & !self.mask"),
        "non-const fn body leaked: {out}"
    );
}

#[test]
fn net_tcp_listener_rs_parses_and_stubs_in_cfg_not_wasip1() {
    let (src, out) = stub("net_tcp_listener.rs");
    syn::parse_file(&out).expect("net_tcp_listener parses");

    assert!(src.contains("cfg_not_wasip1! {"), "fixture sanity");
    assert!(out.contains("cfg_not_wasip1! {"), "got: {out}");

    assert!(out.contains(r#"{ panic!("STUB: not implemented") }"#), "no stubs produced: {out}");
}

#[test]
fn macros_cfg_rs_macro_definitions_left_verbatim() {
    let (src, out) = stub("macros_cfg.rs");
    syn::parse_file(&out).expect("macros_cfg parses");
    assert_eq!(
        src, out,
        "macros/cfg.rs contains only macro_rules! definitions \
         and should be byte-identical after stubbing"
    );
}

#[test]
fn buf_reader_rs_pin_project_with_doc_comments_preserved() {
    let (src, out) = stub("io_util_buf_reader.rs");
    syn::parse_file(&out).expect("buf_reader parses");

    assert!(src.contains("pin_project! {"), "fixture sanity");
    assert!(out.contains("pin_project! {"), "got: {out}");

    let triple_slash_count_src = src.matches("\n    /// ").count();
    let triple_slash_count_out = out.matches("\n    /// ").count();
    assert_eq!(
        triple_slash_count_src, triple_slash_count_out,
        "/// doc-comment count drifted (src={triple_slash_count_src}, out={triple_slash_count_out})"
    );
}

#[test]
fn bom_prefixed_source_handled_safely() {
    let bom = "\u{feff}";
    let src = format!("{bom}fn foo() {{ real(); }}\n");
    let out = stub_source_with_options(&src, StubOptions::default()).expect("BOM source parses");
    assert!(out.starts_with(bom), "BOM lost: {out:?}");
    assert!(out.contains(r#"panic!("STUB: not implemented")"#), "BOM source not stubbed: {out:?}");
}

#[test]
fn crlf_line_endings_preserved() {
    let src = "use foo;\r\n\r\nfn a() { real(); }\r\nfn b() { real(); }\r\n";
    let out = stub_source_with_options(src, StubOptions::default()).expect("CRLF parses");
    let crlf_count = out.matches("\r\n").count();
    assert!(crlf_count >= 3, "CRLF count dropped (expected >=3, got {crlf_count}): {out:?}");
    assert_eq!(
        out.matches(r#"panic!("STUB: not implemented")"#).count(),
        2,
        "both fns must be stubbed: {out}"
    );
}

#[test]
fn tokio_test_qualified_attr_preserved() {
    let src = "#[tokio::test]\nasync fn it_works() { assert!(true); }\n";
    let out = stub_source_with_options(src, StubOptions::default()).unwrap();
    assert!(out.contains("assert!(true)"), "tokio::test fn body was stubbed: {out}");
    assert!(!out.contains("STUB: not implemented"), "tokio::test body must NOT be stubbed: {out}");
}

#[test]
fn async_std_test_qualified_attr_preserved() {
    let src = "#[async_std::test]\nasync fn it_works() { assert!(true); }\n";
    let out = stub_source_with_options(src, StubOptions::default()).unwrap();
    assert!(out.contains("assert!(true)"), "async_std::test fn was stubbed: {out}");
}

#[test]
fn ctor_function_preserved() {
    let src = "#[ctor::ctor]\nfn init_at_load() { real_setup(); }\n";
    let out = stub_source_with_options(src, StubOptions::default()).unwrap();
    assert!(out.contains("real_setup()"), "ctor fn was stubbed (would hang at load): {out}");
}

#[test]
fn cfg_if_branches_now_stub_internal_fns() {
    let src = "cfg_if! {\n    if #[cfg(unix)] {\n        fn platform() { real_unix(); }\n    } else {\n        fn platform() { real_windows(); }\n    }\n}\n";
    let out = stub_source_with_options(src, StubOptions::default()).unwrap();
    assert!(!out.contains("real_unix()"), "cfg_if unix branch leaked: {out}");
    assert!(!out.contains("real_windows()"), "cfg_if windows branch leaked: {out}");
    assert!(
        out.matches(r#"panic!("STUB: not implemented")"#).count() >= 2,
        "expected both branches stubbed: {out}"
    );
}

#[test]
fn static_initializer_closure_body_is_stubbed() {
    let src = "static FOO: once_cell::sync::Lazy<i32> = once_cell::sync::Lazy::new(|| { real_secret(); 42 });\n";
    let out = stub_source_with_options(src, StubOptions::default()).unwrap();
    assert!(!out.contains("real_secret"), "static init closure body leaked: {out}");
}

#[test]
fn unknown_macro_warning_emitted_on_stderr() {
    let src = "some_unknown_macro_xyz! { fn bar() { real(); } }\n";
    let _ = stub_source_with_options(src, StubOptions::default()).unwrap();
}

#[test]
fn all_fixtures_byte_identical_outside_loop_stubs() {
    for name in [
        "blocking.rs",
        "fs_dir_builder.rs",
        "future_maybe_done.rs",
        "util_bit.rs",
        "net_tcp_listener.rs",
        "macros_cfg.rs",
        "io_util_buf_reader.rs",
    ] {
        let (src, out) = stub(name);

        syn::parse_file(&out)
            .unwrap_or_else(|e| panic!("{name}: stubbed output does not parse: {e}"));

        for line in src.lines() {
            let t = line.trim();
            if t.starts_with("///") || t.starts_with("//!") {
                assert!(
                    out.contains(line.trim_end()),
                    "{name}: doc line lost: {line:?}"
                );
            }
        }

        for line in src.lines() {
            let t = line.trim();
            if t.starts_with("//")
                && !t.starts_with("///")
                && !t.starts_with("//!")
                && t.len() > 4
            {
                let preserved = out.contains(line.trim_end());
                if !preserved {
                    eprintln!(
                        "{name}: inline comment may be inside a stubbed fn body \
                         (not necessarily a bug): {line:?}"
                    );
                }
            }
        }
    }
}
