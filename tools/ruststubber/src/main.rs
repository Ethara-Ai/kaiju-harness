use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};
use std::process;

use clap::Parser;
use walkdir::WalkDir;

use ruststubber::cfg_test::attr_implies_test_gate;
use ruststubber::{stub_source_with_options, StubOptions};

/// Rust function body stubber.
///
/// Replaces function bodies with `panic!("STUB: not implemented")` while
/// preserving test functions, `fn main()`, trait declarations, and
/// `#[cfg(test)]` modules — including files reachable via
/// `#[cfg(test)] mod foo;` from `lib.rs` / `main.rs` / `bin/*.rs`.
#[derive(Parser, Debug)]
#[command(name = "ruststubber", version, about)]
struct Cli {
    /// Input directory containing Rust source files.
    #[arg(long)]
    input_dir: PathBuf,

    /// Output directory for stubbed files. Mutually exclusive with --in-place.
    #[arg(long, conflicts_with = "in_place")]
    output_dir: Option<PathBuf>,

    /// Modify files in place instead of writing to an output directory.
    #[arg(long, conflicts_with = "output_dir")]
    in_place: bool,

    /// Preserve doc comments, `//!`/`///` lines, `#[doc = "..."]` attributes
    /// AND ordinary `//` and `/* */` comments in the stubbed output.
    ///
    /// Default behaviour (when this flag is absent) is to strip them all so
    /// the agent sees only signatures, non-doc attributes, test code, and
    /// `panic!("STUB: not implemented")` bodies. Pass `--keep-docs` only when
    /// you explicitly want the upstream comments preserved.
    #[arg(long)]
    keep_docs: bool,
}

fn is_in_target_dir(path: &Path) -> bool {
    path.components().any(|c| c.as_os_str() == "target")
}

/// Path-based fallback: files named `tests.rs` (file-level test module) or
/// living under a `tests/` or `test/` directory are test code by Rust
/// convention even when no `lib.rs` is reachable (e.g. a sub-crate fragment).
fn is_test_module_path(path: &Path) -> bool {
    if matches!(path.file_stem().and_then(|s| s.to_str()), Some("tests")) {
        return true;
    }
    path.components().any(|c| {
        let s = c.as_os_str().to_string_lossy();
        s == "tests" || s == "test"
    })
}

/// Resolve candidate filesystem paths for an external `mod <name>;`.
///
/// Respects `#[path = "custom.rs"]` attribute (which Rust allows on any mod
/// declaration). Without #[path], falls back to conventional resolution:
///   - From `<dir>/lib.rs|main.rs|mod.rs` declaring `mod foo;`:
///       <dir>/foo.rs and <dir>/foo/mod.rs
///   - From `<dir>/bar.rs` declaring `mod foo;`:
///       <dir>/bar/foo.rs and <dir>/bar/foo/mod.rs
fn resolve_mod_candidate_files(
    parent_file: &Path,
    item_mod: &syn::ItemMod,
) -> Vec<PathBuf> {
    let parent_dir = match parent_file.parent() {
        Some(d) => d.to_path_buf(),
        None => return Vec::new(),
    };

    // #[path = "..."] overrides conventional resolution. The path is
    // interpreted relative to the parent file's directory.
    if let Some(custom) = extract_path_attr(&item_mod.attrs) {
        return vec![parent_dir.join(custom)];
    }

    let mod_name = item_mod.ident.to_string();
    let stem = parent_file.file_stem().and_then(|s| s.to_str()).unwrap_or("");
    let owner_dir = if matches!(stem, "lib" | "main" | "mod") {
        parent_dir.clone()
    } else {
        parent_dir.join(stem)
    };
    vec![
        owner_dir.join(format!("{mod_name}.rs")),
        owner_dir.join(&mod_name).join("mod.rs"),
    ]
}

/// Extract a `#[path = "..."]` attribute value if present.
fn extract_path_attr(attrs: &[syn::Attribute]) -> Option<String> {
    attrs.iter().find_map(|a| {
        if !a.path().is_ident("path") { return None }
        let syn::Meta::NameValue(nv) = &a.meta else { return None };
        let syn::Expr::Lit(lit) = &nv.value else { return None };
        let syn::Lit::Str(s) = &lit.lit else { return None };
        Some(s.value())
    })
}

/// Recursively walk from `file_path`. For each `mod foo;` declaration:
///   - if it (or any ancestor in this recursion path) is `#[cfg(test)]`-gated,
///     resolve foo's files and add them to `gated`;
///   - then recurse into foo so transitively reachable submodules of a test
///     module are also recognised as test code (Rust cfg attributes propagate
///     to descendants of a gated mod).
fn scan_for_test_gated_files(
    file_path: &Path,
    gated: &mut HashSet<PathBuf>,
    visited: &mut HashSet<PathBuf>,
    inherited_under_test: bool,
) {
    let canon = fs::canonicalize(file_path).unwrap_or_else(|_| file_path.to_path_buf());
    if !visited.insert(canon) {
        return;
    }
    let Ok(source) = fs::read_to_string(file_path) else { return };
    let Ok(file) = syn::parse_file(&source) else { return };

    for item in &file.items {
        let syn::Item::Mod(item_mod) = item else { continue };
        // Inline body (`mod foo { ... }`) has no external file. The
        // in-file stubber's fold_item_mod already preserves cfg(test) inline
        // modules, so we don't need to track them here.
        if item_mod.content.is_some() {
            continue;
        }
        let attr_gated = item_mod.attrs.iter().any(attr_implies_test_gate);
        let this_gated = inherited_under_test || attr_gated;
        for candidate in resolve_mod_candidate_files(file_path, item_mod) {
            if !candidate.exists() {
                continue;
            }
            let cand_canon = fs::canonicalize(&candidate)
                .unwrap_or_else(|_| candidate.clone());
            if this_gated {
                gated.insert(cand_canon);
            }
            scan_for_test_gated_files(&candidate, gated, visited, this_gated);
        }
    }
}

/// Walk up from input_dir to find the nearest Cargo.toml.
fn find_cargo_manifest(input_dir: &Path) -> Option<PathBuf> {
    let mut dir = fs::canonicalize(input_dir).ok()?;
    loop {
        let cand = dir.join("Cargo.toml");
        if cand.is_file() {
            return Some(cand);
        }
        dir = dir.parent()?.to_path_buf();
    }
}

/// Shell out to `cargo metadata --no-deps --format-version=1` and extract
/// the actual crate-root source paths cargo considers part of each target.
///
/// This is the authoritative source of truth and handles:
///   - `[lib].path = "src/custom.rs"` overrides
///   - Workspace members declared in `[workspace] members = [...]`
///   - Cfg flags set by build.rs via `cargo:rustc-cfg=...` (cargo evaluates
///     build.rs and the resulting cfg flags are reflected in the metadata).
///   - Custom `[[bin]] path = "..."` declarations
///
/// Returns None when there's no Cargo.toml or cargo isn't installed/working,
/// so the caller can fall back to a filesystem walk.
fn find_crate_roots_via_cargo(input_dir: &Path) -> Option<Vec<PathBuf>> {
    let manifest = find_cargo_manifest(input_dir)?;
    let output = std::process::Command::new("cargo")
        .args(["metadata", "--no-deps", "--format-version", "1"])
        .arg("--manifest-path")
        .arg(&manifest)
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let json = std::str::from_utf8(&output.stdout).ok()?;
    let roots = parse_cargo_metadata_roots(json);
    if roots.is_empty() { None } else { Some(roots) }
}

/// Parse cargo metadata JSON and return crate-root source paths.
///
/// Includes only targets of kind: lib / rlib / cdylib / dylib / staticlib /
/// proc-macro / bin. Excludes test / example / bench / custom-build targets
/// (those are NOT crate roots and should be stubbed normally if anything).
///
/// Public so it can be unit-tested without invoking the real cargo binary.
fn parse_cargo_metadata_roots(json: &str) -> Vec<PathBuf> {
    let allowed_kinds = ["lib", "bin", "rlib", "cdylib", "dylib", "staticlib", "proc-macro"];
    let mut roots: Vec<PathBuf> = Vec::new();
    let mut search_pos = 0;
    while let Some(rel) = json[search_pos..].find("\"src_path\":\"") {
        let abs_match = search_pos + rel;
        let val_start = abs_match + "\"src_path\":\"".len();
        let bytes = json.as_bytes();
        let mut end = val_start;
        while end < bytes.len() {
            if bytes[end] == b'\\' && end + 1 < bytes.len() {
                end += 2;
                continue;
            }
            if bytes[end] == b'"' { break; }
            end += 1;
        }
        let path_str = &json[val_start..end];
        // Find the nearest preceding "kind":[...] within an 800-byte window.
        let window_start = abs_match.saturating_sub(800);
        let preceding = &json[window_start..abs_match];
        let mut is_root = false;
        if let Some(kind_pos) = preceding.rfind("\"kind\":[") {
            let after = &preceding[kind_pos + "\"kind\":[".len()..];
            if let Some(close) = after.find(']') {
                let kinds_section = &after[..close];
                is_root = allowed_kinds.iter().any(|k| {
                    let needle = format!("\"{}\"", k);
                    kinds_section.contains(&needle)
                });
            }
        }
        if is_root {
            let unescaped = path_str.replace("\\\\", "\\").replace("\\\"", "\"");
            roots.push(PathBuf::from(unescaped));
        }
        search_pos = end.max(val_start + 1);
    }
    roots.sort();
    roots.dedup();
    roots
}

/// Filesystem-only crate-root discovery (fallback when Cargo.toml is absent
/// or cargo can't be run — e.g. running ruststubber on a `src/` directory
/// extracted in isolation, or a partially-set-up workspace).
fn find_crate_roots_via_filesystem(input_dir: &Path) -> Vec<PathBuf> {
    let mut roots = Vec::new();
    for entry in WalkDir::new(input_dir).into_iter().filter_map(|e| e.ok()) {
        if !entry.file_type().is_file() {
            continue;
        }
        let path = entry.path();
        if is_in_target_dir(path) {
            continue;
        }
        let name = entry.file_name();
        let is_bin_root = path
            .parent()
            .and_then(|p| p.file_name())
            .and_then(|n| n.to_str())
            == Some("bin")
            && path.extension().and_then(|x| x.to_str()) == Some("rs");
        if name == "lib.rs" || name == "main.rs" || is_bin_root {
            roots.push(path.to_path_buf());
        }
    }
    roots
}

/// Set of `.rs` files under `input_dir` that belong to a `#[cfg(test)]`-gated
/// module reachable from any crate root.
///
/// Two-tier resolution strategy for maximum coverage:
///   1. If Cargo.toml exists at or above `input_dir` AND `cargo` is installed,
///      ask cargo itself via `cargo metadata --no-deps`. This handles
///      custom `[lib].path`, workspace members, build.rs-set cfg flags, and
///      custom `[[bin]] path = "..."` declarations — i.e. closes the previously
///      out-of-scope Gaps 2 and 3.
///   2. Otherwise (or if cargo fails), walk the filesystem for lib.rs / main.rs
///      / bin/*.rs. This is the original fallback that handles subdirectory
///      inputs and crates that don't compile cleanly.
pub fn is_proc_macro_crate(input_dir: &Path) -> bool {
    let Some(manifest) = find_cargo_manifest(input_dir) else { return false };
    let Ok(contents) = fs::read_to_string(&manifest) else { return false };
    cargo_toml_declares_proc_macro(&contents)
}

fn cargo_toml_declares_proc_macro(contents: &str) -> bool {
    let mut in_lib_section = false;
    for raw in contents.lines() {
        let line = raw.split('#').next().unwrap_or("").trim();
        if line.is_empty() { continue }
        if let Some(stripped) = line.strip_prefix('[') {
            let header = stripped.trim_end_matches(']').trim();
            in_lib_section = header == "lib";
            continue;
        }
        if in_lib_section {
            let eq = match line.find('=') { Some(i) => i, None => continue };
            let key = line[..eq].trim();
            let val = line[eq + 1 ..].trim().trim_matches(',');
            if key == "proc-macro" || key == "proc_macro" {
                return val == "true";
            }
        }
    }
    false
}

fn collect_test_gated_files(input_dir: &Path) -> HashSet<PathBuf> {
    let mut gated: HashSet<PathBuf> = HashSet::new();
    let mut visited: HashSet<PathBuf> = HashSet::new();

    let roots = find_crate_roots_via_cargo(input_dir)
        .unwrap_or_else(|| find_crate_roots_via_filesystem(input_dir));

    for root in roots {
        if root.exists() {
            scan_for_test_gated_files(&root, &mut gated, &mut visited, false);
        }
    }

    gated
}

fn main() {
    let cli = Cli::parse();

    if !cli.in_place && cli.output_dir.is_none() {
        eprintln!("Error: must specify either --output-dir or --in-place");
        process::exit(1);
    }

    let input_dir = &cli.input_dir;
    if !input_dir.is_dir() {
        eprintln!("Error: input directory does not exist: {}", input_dir.display());
        process::exit(1);
    }

    if is_proc_macro_crate(input_dir) {
        eprintln!(
            "ruststubber: detected proc-macro crate at {} — skipping entirely",
            input_dir.display()
        );
        eprintln!("ruststubber: 0 stubbed, 0 non-rs copied, 0 test-modules skipped, 0 errors");
        return;
    }

    let test_gated = collect_test_gated_files(input_dir);
    if !test_gated.is_empty() {
        eprintln!(
            "ruststubber: detected {} test-gated module file(s) via #[cfg(test)] mod analysis",
            test_gated.len()
        );
    }

    let mut errors = 0u32;
    let mut stubbed = 0u32;
    let mut copied = 0u32;
    let mut skipped_test = 0u32;

    for entry in WalkDir::new(input_dir).into_iter().filter_map(|e| e.ok()) {
        let src_path = entry.path();
        if src_path.is_dir() {
            continue;
        }

        let rel_path = src_path.strip_prefix(input_dir).unwrap_or(src_path);
        if is_in_target_dir(rel_path) {
            continue;
        }

        let dest_path = if cli.in_place {
            src_path.to_path_buf()
        } else {
            cli.output_dir.as_ref().unwrap().join(rel_path)
        };

        if let Some(parent) = dest_path.parent() {
            if !parent.exists() {
                if let Err(e) = fs::create_dir_all(parent) {
                    eprintln!("Error creating directory {}: {e}", parent.display());
                    errors += 1;
                    continue;
                }
            }
        }

        // Two independent signals — belt-and-suspenders:
        //   1. Semantic scan from lib.rs/main.rs (handles arbitrary names).
        //   2. Path heuristic (handles sub-crate fragments where no crate
        //      root is visible to the scanner).
        let canon_src = fs::canonicalize(src_path).unwrap_or_else(|_| src_path.to_path_buf());
        let semantic_says_test = test_gated.contains(&canon_src);
        let path_says_test = is_test_module_path(rel_path);
        if semantic_says_test || path_says_test {
            // In output-dir mode preserve the original .rs verbatim so the
            // resulting tree still compiles. In in-place mode just leave the
            // file alone (it's already correct on disk).
            if !cli.in_place && src_path.extension().and_then(|e| e.to_str()) == Some("rs") {
                if let Err(e) = fs::copy(src_path, &dest_path) {
                    eprintln!("Error copying skipped test file {}: {e}", src_path.display());
                    errors += 1;
                }
            }
            skipped_test += 1;
            continue;
        }

        if src_path.extension().and_then(|e| e.to_str()) != Some("rs") {
            if !cli.in_place {
                if let Err(e) = fs::copy(src_path, &dest_path) {
                    eprintln!("Error copying {}: {e}", src_path.display());
                    errors += 1;
                } else {
                    copied += 1;
                }
            }
            continue;
        }

        let source = match fs::read_to_string(src_path) {
            Ok(s) => s,
            Err(e) => {
                eprintln!("Error reading {}: {e}", src_path.display());
                errors += 1;
                continue;
            }
        };

        let options = StubOptions { strip_docs: !cli.keep_docs };
        match stub_source_with_options(&source, options) {
            Ok(output) => {
                if let Err(e) = fs::write(&dest_path, output) {
                    eprintln!("Error writing {}: {e}", dest_path.display());
                    errors += 1;
                } else {
                    stubbed += 1;
                }
            }
            Err(e) => {
                eprintln!("Error parsing {}: {e}", src_path.display());
                errors += 1;
            }
        }
    }

    eprintln!(
        "ruststubber: {stubbed} stubbed, {copied} non-rs copied, {skipped_test} test-modules skipped, {errors} errors"
    );

    if errors > 0 {
        process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn make_layout(files: &[(&str, &str)]) -> tempfile::TempDir {
        let dir = tempfile::tempdir().expect("tempdir");
        for (rel, content) in files {
            let p = dir.path().join(rel);
            if let Some(parent) = p.parent() {
                fs::create_dir_all(parent).expect("mkdir");
            }
            fs::write(&p, content).expect("write");
        }
        dir
    }

    #[test]
    fn proc_macro_true_detected() {
        let cargo = r#"
[package]
name = "x"
version = "0.0.1"
edition = "2021"

[lib]
proc-macro = true
"#;
        assert!(cargo_toml_declares_proc_macro(cargo));
    }

    #[test]
    fn proc_macro_false_or_absent_not_detected() {
        assert!(!cargo_toml_declares_proc_macro("[package]\nname = \"x\"\n"));
        assert!(!cargo_toml_declares_proc_macro("[lib]\nproc-macro = false\n"));
        assert!(!cargo_toml_declares_proc_macro("[dependencies]\nproc-macro = true\n"));
        assert!(!cargo_toml_declares_proc_macro(""));
    }

    #[test]
    fn proc_macro_underscore_variant_detected() {
        let cargo = "[lib]\nproc_macro = true\n";
        assert!(cargo_toml_declares_proc_macro(cargo));
    }

    #[test]
    fn proc_macro_with_comment_detected() {
        let cargo = "[lib]\nproc-macro = true # this is a proc macro crate\n";
        assert!(cargo_toml_declares_proc_macro(cargo));
    }

    #[test]
    fn proc_macro_with_spaces_in_header_detected() {
        let cargo = "[ lib ]\nproc-macro = true\n";
        assert!(cargo_toml_declares_proc_macro(cargo));
    }

    #[test]
    fn lib_dot_subsection_not_detected_as_lib() {
        let cargo = "[lib.metadata]\nproc-macro = true\n";
        assert!(!cargo_toml_declares_proc_macro(cargo));
    }

    #[test]
    fn is_proc_macro_crate_walks_up_to_find_manifest() {
        let dir = make_layout(&[
            ("Cargo.toml", "[package]\nname = \"x\"\nversion = \"0\"\nedition = \"2021\"\n\n[lib]\nproc-macro = true\n"),
            ("src/lib.rs", "pub fn foo() {}\n"),
        ]);
        assert!(is_proc_macro_crate(&dir.path().join("src")));
        assert!(is_proc_macro_crate(dir.path()));
    }

    #[test]
    fn is_proc_macro_crate_returns_false_for_regular_lib() {
        let dir = make_layout(&[
            ("Cargo.toml", "[package]\nname = \"x\"\nversion = \"0\"\nedition = \"2021\"\n"),
            ("src/lib.rs", "pub fn foo() {}\n"),
        ]);
        assert!(!is_proc_macro_crate(&dir.path().join("src")));
    }

    #[test]
    fn path_heuristic_matches_tests_rs() {
        assert!(is_test_module_path(Path::new("src/tests.rs")));
        assert!(is_test_module_path(Path::new("tests.rs")));
        assert!(is_test_module_path(Path::new("src/tests/foo.rs")));
        assert!(is_test_module_path(Path::new("foo/test/bar.rs")));
    }

    #[test]
    fn path_heuristic_rejects_lookalikes() {
        assert!(!is_test_module_path(Path::new("src/lib.rs")));
        assert!(!is_test_module_path(Path::new("src/interfaces.rs")));
        assert!(!is_test_module_path(Path::new("src/test_helpers_not_gated.rs")));
        assert!(!is_test_module_path(Path::new("src/testing.rs")));
    }

    #[test]
    fn resolve_mod_files_from_lib_rs() {
        let lib = PathBuf::from("src/lib.rs");
        let item_mod: syn::ItemMod = syn::parse_quote! { mod tests; };
        let cands = resolve_mod_candidate_files(&lib, &item_mod);
        assert!(cands.iter().any(|c| c.ends_with("src/tests.rs")));
        assert!(cands.iter().any(|c| c.ends_with("src/tests/mod.rs")));
    }

    #[test]
    fn resolve_mod_files_from_nested_file() {
        let f = PathBuf::from("src/foo.rs");
        let item_mod: syn::ItemMod = syn::parse_quote! { mod bar; };
        let cands = resolve_mod_candidate_files(&f, &item_mod);
        assert!(cands.iter().any(|c| c.ends_with("src/foo/bar.rs")));
        assert!(cands.iter().any(|c| c.ends_with("src/foo/bar/mod.rs")));
    }

    #[test]
    fn semantic_scan_finds_getifs_layout() {
        // Mirrors the al8n/getifs failure: lib.rs gates `mod tests`, tests.rs
        // has un-attributed sub-modules whose function bodies have NO
        // #[cfg(test)] on themselves. All three files MUST be detected.
        let dir = make_layout(&[
            ("lib.rs", "#[cfg(all(test, not(windows)))]\nmod tests;\nmod interfaces;\n"),
            ("tests.rs", "#[cfg(bsd_like)]\nmod bsd;\n#[cfg(linux_like)]\nmod linux;\nstruct TestInterface;\n"),
            ("tests/bsd.rs", "fn helper() {}\n"),
            ("tests/linux.rs", "fn other() {}\n"),
            ("interfaces.rs", "pub fn list() -> Vec<u8> { Vec::new() }\n"),
        ]);
        let gated = collect_test_gated_files(dir.path());
        let names: Vec<String> = gated.iter()
            .map(|p| p.file_name().unwrap().to_string_lossy().into_owned())
            .collect();
        assert!(names.contains(&"tests.rs".to_string()), "tests.rs missing: {names:?}");
        assert!(names.contains(&"bsd.rs".to_string()), "tests/bsd.rs missing: {names:?}");
        assert!(names.contains(&"linux.rs".to_string()), "tests/linux.rs missing: {names:?}");
        assert!(!names.contains(&"interfaces.rs".to_string()), "interfaces.rs wrongly gated");
    }

    #[test]
    fn semantic_scan_works_with_arbitrary_name() {
        // Path heuristic wouldn't catch `mod my_helpers;` (no 'tests' in path),
        // but semantic scan must.
        let dir = make_layout(&[
            ("lib.rs", "#[cfg(test)]\nmod my_helpers;\nmod prod;\n"),
            ("my_helpers.rs", "fn helper() {}\n"),
            ("prod.rs", "pub fn x() -> u8 { 0 }\n"),
        ]);
        let gated = collect_test_gated_files(dir.path());
        let names: Vec<String> = gated.iter()
            .map(|p| p.file_name().unwrap().to_string_lossy().into_owned())
            .collect();
        assert!(names.contains(&"my_helpers.rs".to_string()));
        assert!(!names.contains(&"prod.rs".to_string()));
    }

    #[test]
    fn semantic_scan_empty_when_no_test_mods() {
        let dir = make_layout(&[
            ("lib.rs", "mod a;\nmod b;\n"),
            ("a.rs", "fn a() {}\n"),
            ("b.rs", "fn b() {}\n"),
        ]);
        let gated = collect_test_gated_files(dir.path());
        assert!(gated.is_empty(), "expected empty, got {gated:?}");
    }

    #[test]
    fn semantic_scan_skips_inline_mod_bodies() {
        let dir = make_layout(&[
            ("lib.rs", "#[cfg(test)]\nmod inline_tests { fn helper() {} }\nmod outside_file;\n"),
            ("outside_file.rs", "fn keep_me() {}\n"),
        ]);
        let gated = collect_test_gated_files(dir.path());
        assert!(gated.is_empty(), "inline body has no external file; outside_file is prod");
    }

    #[test]
    fn semantic_scan_resolves_via_mod_rs() {
        let dir = make_layout(&[
            ("lib.rs", "#[cfg(test)]\nmod tests;\n"),
            ("tests/mod.rs", "fn x() {}\n#[cfg(bsd)]\nmod child;\n"),
            ("tests/child.rs", "fn y() {}\n"),
        ]);
        let gated = collect_test_gated_files(dir.path());
        let names: Vec<String> = gated.iter()
            .map(|p| p.file_name().unwrap().to_string_lossy().into_owned())
            .collect();
        assert!(names.contains(&"mod.rs".to_string()), "tests/mod.rs missing: {names:?}");
        assert!(names.contains(&"child.rs".to_string()), "tests/child.rs missing: {names:?}");
    }

    // === Hardening tests (Gaps 1-4) ===

    #[test]
    fn attr_implies_test_gate_detects_cfg_test() {
        let attr: syn::Attribute = syn::parse_quote!( #[cfg(test)] );
        assert!(attr_implies_test_gate(&attr));
    }

    #[test]
    fn attr_implies_test_gate_detects_cfg_all_test() {
        let attr: syn::Attribute = syn::parse_quote!( #[cfg(all(test, not(windows)))] );
        assert!(attr_implies_test_gate(&attr));
    }

    #[test]
    fn attr_implies_test_gate_rejects_cfg_not_test() {
        // Gap #3 closure: substring 'test' appears in cfg(not(test)) but it's
        // production-only code. Must NOT be flagged as a test gate.
        let attr: syn::Attribute = syn::parse_quote!( #[cfg(not(test))] );
        assert!(!attr_implies_test_gate(&attr),
            "cfg(not(test)) is production code, not test code");
    }

    #[test]
    fn attr_implies_test_gate_rejects_feature_named_test() {
        // Gap #3 closure: cfg(feature = "test-utils") contains 'test' as a
        // substring of a value, but is feature-gated production code.
        let attr: syn::Attribute = syn::parse_quote!( #[cfg(feature = "test-utils")] );
        assert!(!attr_implies_test_gate(&attr));
    }

    #[test]
    fn attr_implies_test_gate_rejects_any_with_alternative() {
        // Gap #3 closure: any(test, feature=...) can be enabled without test=true,
        // so the module is NOT strictly test-only.
        let attr: syn::Attribute = syn::parse_quote!( #[cfg(any(test, feature = "x"))] );
        assert!(!attr_implies_test_gate(&attr));
    }

    #[test]
    fn attr_implies_test_gate_accepts_cfg_attr_indirection() {
        // Gap #4 closure: cfg_attr applying cfg(test) is still a test gate.
        let attr: syn::Attribute = syn::parse_quote!( #[cfg_attr(unix, cfg(test))] );
        assert!(attr_implies_test_gate(&attr));
    }

    #[test]
    fn resolve_mod_files_respects_path_attribute() {
        // Gap #2 closure: #[path = "..."] overrides conventional resolution.
        let lib = PathBuf::from("crate_a/src/lib.rs");
        let item_mod: syn::ItemMod = syn::parse_quote! {
            #[path = "../my_tests/main.rs"]
            mod tests;
        };
        let cands = resolve_mod_candidate_files(&lib, &item_mod);
        assert_eq!(cands.len(), 1, "with #[path], only one candidate expected: {cands:?}");
        assert!(cands[0].ends_with("../my_tests/main.rs"));
    }

    #[test]
    fn semantic_scan_walks_workspace_members() {
        // Gap #1 closure: a directory with multiple lib.rs files (workspace-style)
        // is fully covered; each crate root is scanned independently.
        let dir = make_layout(&[
            ("crate_a/src/lib.rs", "#[cfg(test)]\nmod helpers;\n"),
            ("crate_a/src/helpers.rs", "fn x() {}\n"),
            ("crate_b/src/lib.rs", "#[cfg(test)]\nmod utils;\n"),
            ("crate_b/src/utils.rs", "fn y() {}\n"),
        ]);
        let gated = collect_test_gated_files(dir.path());
        let names: Vec<String> = gated.iter()
            .map(|p| p.file_name().unwrap().to_string_lossy().into_owned())
            .collect();
        assert!(names.contains(&"helpers.rs".to_string()),
            "crate_a helpers missing: {names:?}");
        assert!(names.contains(&"utils.rs".to_string()),
            "crate_b utils missing: {names:?}");
    }

    // === Cargo-metadata integration tests (close Gaps 1–3 from final pass) ===

    #[test]
    fn parse_cargo_metadata_roots_extracts_lib_target() {
        let json = r#"{"packages":[{"targets":[
            {"kind":["lib"],"src_path":"/path/to/src/lib.rs"}
        ]}]}"#;
        let roots = parse_cargo_metadata_roots(json);
        assert_eq!(roots, vec![PathBuf::from("/path/to/src/lib.rs")]);
    }

    #[test]
    fn parse_cargo_metadata_roots_extracts_bin_and_lib() {
        let json = r#"{"packages":[{"targets":[
            {"kind":["lib"],"src_path":"/p/src/lib.rs"},
            {"kind":["bin"],"src_path":"/p/src/bin/cli.rs"}
        ]}]}"#;
        let roots = parse_cargo_metadata_roots(json);
        assert_eq!(roots.len(), 2, "got: {roots:?}");
        assert!(roots.contains(&PathBuf::from("/p/src/lib.rs")));
        assert!(roots.contains(&PathBuf::from("/p/src/bin/cli.rs")));
    }

    #[test]
    fn parse_cargo_metadata_roots_skips_test_example_bench_targets() {
        // Closes Gap 3-adjacent concern: cargo emits these kinds for integration
        // tests, examples, benches, and build scripts. None are crate roots,
        // they would otherwise be stubbed if walked as roots.
        let json = r#"{"packages":[{"targets":[
            {"kind":["lib"],"src_path":"/p/src/lib.rs"},
            {"kind":["test"],"src_path":"/p/tests/integration.rs"},
            {"kind":["example"],"src_path":"/p/examples/demo.rs"},
            {"kind":["bench"],"src_path":"/p/benches/perf.rs"},
            {"kind":["custom-build"],"src_path":"/p/build.rs"}
        ]}]}"#;
        let roots = parse_cargo_metadata_roots(json);
        assert_eq!(roots, vec![PathBuf::from("/p/src/lib.rs")]);
    }

    #[test]
    fn parse_cargo_metadata_roots_handles_workspace_members() {
        // Cargo emits one package entry per workspace member. We collect every
        // crate root across all members.
        let json = r#"{"packages":[
            {"targets":[{"kind":["lib"],"src_path":"/ws/crate_a/src/lib.rs"}]},
            {"targets":[{"kind":["bin"],"src_path":"/ws/crate_b/src/main.rs"}]},
            {"targets":[{"kind":["proc-macro"],"src_path":"/ws/crate_c/src/lib.rs"}]}
        ]}"#;
        let roots = parse_cargo_metadata_roots(json);
        assert_eq!(roots.len(), 3, "got: {roots:?}");
    }

    #[test]
    fn parse_cargo_metadata_roots_returns_empty_on_garbage() {
        assert!(parse_cargo_metadata_roots("not json at all").is_empty());
        assert!(parse_cargo_metadata_roots("{}").is_empty());
        assert!(parse_cargo_metadata_roots(r#"{"packages":[]}"#).is_empty());
    }

    #[test]
    fn parse_cargo_metadata_roots_handles_custom_lib_path() {
        // Closes the custom-path gap: cargo returns whatever path the manifest
        // declared, no convention assumption needed.
        let json = r#"{"packages":[{"targets":[
            {"kind":["lib"],"src_path":"/p/source/main_entry.rs"}
        ]}]}"#;
        let roots = parse_cargo_metadata_roots(json);
        assert_eq!(roots, vec![PathBuf::from("/p/source/main_entry.rs")]);
    }

    #[test]
    fn find_crate_roots_via_cargo_returns_none_without_manifest() {
        let dir = make_layout(&[("src/lib.rs", "// no Cargo.toml here")]);
        assert!(find_crate_roots_via_cargo(dir.path()).is_none());
    }

    #[test]
    fn find_crate_roots_via_filesystem_finds_workspace_libs() {
        let dir = make_layout(&[
            ("crate_a/src/lib.rs", "// a"),
            ("crate_b/src/lib.rs", "// b"),
            ("crate_c/src/main.rs", "fn main() {}"),
            ("crate_c/src/bin/extra.rs", "fn main() {}"),
        ]);
        let roots = find_crate_roots_via_filesystem(dir.path());
        let names: Vec<String> = roots.iter()
            .map(|p| p.file_name().unwrap().to_string_lossy().into_owned())
            .collect();
        assert_eq!(names.iter().filter(|n| n.as_str() == "lib.rs").count(), 2,
            "two lib.rs expected: {names:?}");
        assert!(names.contains(&"main.rs".to_string()));
        assert!(names.contains(&"extra.rs".to_string()));
    }
}
