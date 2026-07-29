#include "stubber.hpp"

#include "clang/Tooling/CommonOptionsParser.h"
#include "clang/Tooling/Tooling.h"
#include "llvm/Support/CommandLine.h"
#include "llvm/Support/FileSystem.h"
#include "llvm/Support/Path.h"
#include "llvm/Support/raw_ostream.h"

#include <array>
#include <cstdio>
#include <string>
#include <system_error>
#include <vector>

using namespace clang::tooling;
using namespace llvm;

static cl::OptionCategory StubberCategory("cppstubber options");

static cl::opt<std::string> InputDir(
    "input-dir",
    cl::desc("Directory to recursively stub (alternative to compile_commands)"),
    cl::value_desc("path"), cl::cat(StubberCategory));

static cl::opt<bool> InPlace("in-place",
                             cl::desc("Modify files in place (default: false)"),
                             cl::cat(StubberCategory));

static cl::opt<bool> StubPrivate(
    "stub-private",
    cl::desc("Also stub private methods (default: true)"),
    cl::init(true), cl::cat(StubberCategory));

static cl::opt<bool> Quiet("quiet", cl::desc("Suppress per-file output"),
                           cl::cat(StubberCategory));

static cl::opt<std::string> CxxStd(
    "std",
    cl::desc("C++ standard for parsing headers (default: c++20)"),
    cl::init("c++20"), cl::value_desc("std"), cl::cat(StubberCategory));

static cl::list<std::string> ExtraArgs(
    "stub-extra-arg",
    cl::desc("Extra compiler arg appended to every parse (repeatable) — e.g. "
             "-isysroot/-I for a specific toolchain; mirrors clang-tidy's flag"),
    cl::value_desc("arg"), cl::cat(StubberCategory));

// The platform SDK sysroot. Without it, system headers (`<string>`, `<vector>`,
// …) don't resolve, the parse errors early, and EVERY subsequent function is
// silently skipped — the real reason header-only libs yielded 0 stubs. On Apple
// clang-as-libTooling does NOT auto-add the SDK, so we query it once via xcrun.
// On Linux the system headers are on the default path, so this returns "".
static std::string detectSysroot() {
#ifdef __APPLE__
    std::array<char, 1024> buf{};
    std::string out;
    if (FILE *pipe = popen("xcrun --show-sdk-path 2>/dev/null", "r")) {
        while (fgets(buf.data(), buf.size(), pipe))
            out += buf.data();
        pclose(pipe);
    }
    while (!out.empty() && (out.back() == '\n' || out.back() == '\r'))
        out.pop_back();
    return out;
#else
    return "";
#endif
}

static bool isCppFile(StringRef path) {
    auto ext = sys::path::extension(path).lower();
    return ext == ".cpp" || ext == ".cc" || ext == ".cxx" || ext == ".c++" ||
           ext == ".hpp" || ext == ".hh" || ext == ".hxx" || ext == ".h++" ||
           ext == ".h";
}

// Include roots so a standalone header resolves its own `#include "pkg/x.hpp"`.
// Header-only libraries include siblings relative to an include root (the repo
// root, or an `include/`/`src/` dir), so without these `-I` flags the AST is
// nearly empty and the stubber "finds 0 functions" — the real header-only bug,
// not the API rename. We add the input dir, its common source roots, and every
// immediate subdirectory (covers `include/<pkg>/…` layouts).
static std::vector<std::string> includeDirs(StringRef dir) {
    std::vector<std::string> roots;
    auto add = [&](const std::string &p) {
        if (sys::fs::is_directory(p))
            roots.push_back(p);
    };
    add(dir.str());
    for (const char *sub : {"include", "src", "source", "sources", "lib"}) {
        SmallString<256> p(dir);
        sys::path::append(p, sub);
        add(std::string(p));
    }
    std::error_code ec;
    for (sys::fs::directory_iterator it(dir, ec), end; it != end && !ec;
         it.increment(ec)) {
        if (sys::fs::is_directory(it->path())) {
            StringRef name = sys::path::filename(it->path());
            if (name != "build" && name != ".git" && name != "test" &&
                name != "tests" && name != "third_party" && name != "vendor" &&
                name != "extern" && name != "_deps")
                roots.push_back(it->path());
        }
    }
    return roots;
}

static std::vector<std::string> collectCppFiles(StringRef dir) {
    std::vector<std::string> files;
    std::error_code ec;
    for (sys::fs::recursive_directory_iterator it(dir, ec), end;
         it != end && !ec; it.increment(ec)) {
        auto &entry = *it;
        StringRef path = entry.path();

        if (path.contains("/build/") || path.contains("/cmake-build-") ||
            path.contains("/builddir/") || path.contains("/.cache/") ||
            path.contains("/_deps/") || path.contains("/third_party/") ||
            path.contains("/vendor/") || path.contains("/extern/") ||
            path.contains("/.git/"))
            continue;

        if (isCppFile(path))
            files.push_back(path.str());
    }
    return files;
}

int main(int argc, const char **argv) {
    cppstubber::StubConfig config;

    if (argc > 1 && std::string(argv[1]).find("--input-dir") != std::string::npos) {
        cl::ParseCommandLineOptions(argc, argv, "C++ Function Stubber\n");

        config.in_place = InPlace;
        config.stub_private = StubPrivate;

        if (InputDir.empty()) {
            errs() << "Error: --input-dir is required in directory mode\n";
            return 1;
        }

        auto files = collectCppFiles(InputDir);
        if (files.empty()) {
            errs() << "No C++ files found in: " << InputDir << "\n";
            return 1;
        }

        // Base compile args every file is parsed with: FORCE C++ (`-x c++` — a
        // `.h` file otherwise parses as C, so all template/class code is a parse
        // error and yields no functions), a modern standard, and the include
        // roots. `-ferror-limit=0` keeps clang building the AST past the
        // inevitable missing-3rd-party-header errors instead of bailing early.
        std::vector<std::string> baseArgs = {
            "-x", "c++", "-std=" + CxxStd, "-ferror-limit=0",
            "-Wno-everything", "-fsyntax-only"};
        std::string sysroot = detectSysroot();
        if (!sysroot.empty()) {
            baseArgs.push_back("-isysroot");
            baseArgs.push_back(sysroot);
        }
        for (const auto &inc : includeDirs(InputDir))
            baseArgs.push_back("-I" + inc);
        for (const auto &ea : ExtraArgs)
            baseArgs.push_back(ea);

        unsigned total_stubbed = 0;
        unsigned total_skipped = 0;
        unsigned files_processed = 0;

        for (const auto &file : files) {
            std::vector<std::string> args = {"cppstubber", file, "--"};
            for (const auto &a : baseArgs)
                args.push_back(a);
            std::vector<const char *> argv_vec;
            for (auto &a : args)
                argv_vec.push_back(a.c_str());

            int fake_argc = static_cast<int>(argv_vec.size());
            auto parser = CommonOptionsParser::create(
                fake_argc, argv_vec.data(), StubberCategory);
            if (!parser) {
                if (!Quiet)
                    errs() << "  SKIP (parse failed): " << file << "\n";
                continue;
            }

            ClangTool tool(parser->getCompilations(),
                           parser->getSourcePathList());

            cppstubber::StubActionFactory factory(config);
            tool.run(&factory);

            total_stubbed += factory.getTotalStubs();
            total_skipped += factory.getTotalSkips();
            ++files_processed;

            if (!Quiet && factory.getTotalStubs() > 0) {
                outs() << "  STUBBED " << factory.getTotalStubs()
                       << " functions in: " << file << "\n";
            }
        }

        outs() << "\ncppstubber summary:\n"
               << "  Files processed: " << files_processed << "\n"
               << "  Functions stubbed: " << total_stubbed << "\n"
               << "  Functions skipped: " << total_skipped << "\n";

        return 0;
    }

    auto parser =
        CommonOptionsParser::create(argc, argv, StubberCategory);
    if (!parser) {
        errs() << "Error: " << toString(parser.takeError()) << "\n";
        return 1;
    }

    config.in_place = InPlace;
    config.stub_private = StubPrivate;

    ClangTool tool(parser->getCompilations(), parser->getSourcePathList());

    cppstubber::StubActionFactory factory(config);
    int result = tool.run(&factory);

    if (!Quiet) {
        outs() << "\ncppstubber summary:\n"
               << "  Functions stubbed: " << factory.getTotalStubs() << "\n"
               << "  Functions skipped: " << factory.getTotalSkips() << "\n";
    }

    return result;
}
