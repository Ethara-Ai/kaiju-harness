#ifndef CPPSTUBBER_STUBBER_HPP
#define CPPSTUBBER_STUBBER_HPP

#include "clang/AST/ASTConsumer.h"
#include "clang/AST/RecursiveASTVisitor.h"
#include "clang/Frontend/CompilerInstance.h"
#include "clang/Frontend/FrontendAction.h"
#include "clang/Rewrite/Core/Rewriter.h"
#include "clang/Tooling/Tooling.h"

#include <string>
#include <unordered_set>

namespace cppstubber {

/// Configuration controlling what gets stubbed.
struct StubConfig {
    /// Stub regular function bodies with: std::abort(); /* STUB: not implemented */
    std::string stub_marker = "std::abort(); /* STUB: not implemented */";
    /// Stub constexpr/consteval function bodies with: return {};
    std::string constexpr_marker = "return {};";
    /// Stub noexcept function bodies with: std::abort();
    std::string noexcept_marker = "abort();";
    /// If true, stub all functions including private methods
    bool stub_private = true;
    /// If true, operate in-place (overwrite source files)
    bool in_place = false;
};

// ──────────────────────────────────────────────────────────
// AST Visitor — walks declarations, replaces function bodies
// ──────────────────────────────────────────────────────────

class StubVisitor : public clang::RecursiveASTVisitor<StubVisitor> {
public:
    explicit StubVisitor(clang::Rewriter &R, const StubConfig &cfg,
                         clang::ASTContext &ctx);

    bool VisitFunctionDecl(clang::FunctionDecl *FD);

    unsigned getStubCount() const { return stub_count_; }
    unsigned getSkipCount() const { return skip_count_; }

private:
    bool shouldSkip(const clang::FunctionDecl *FD) const;
    bool isTestRelated(const clang::FunctionDecl *FD) const;
    std::string chooseMarker(const clang::FunctionDecl *FD) const;

    clang::Rewriter &rewriter_;
    const StubConfig &config_;
    clang::ASTContext &context_;
    unsigned stub_count_ = 0;
    unsigned skip_count_ = 0;
};

// ──────────────────────────────────────────────────────────
// AST Consumer — owns the visitor
// ──────────────────────────────────────────────────────────

class StubConsumer : public clang::ASTConsumer {
public:
    StubConsumer(clang::Rewriter &R, const StubConfig &cfg,
                 clang::ASTContext &ctx);
    void HandleTranslationUnit(clang::ASTContext &ctx) override;

    unsigned getStubCount() const { return visitor_.getStubCount(); }
    unsigned getSkipCount() const { return visitor_.getSkipCount(); }

private:
    StubVisitor visitor_;
};

// ──────────────────────────────────────────────────────────
// Frontend Action — creates the consumer, writes results
// ──────────────────────────────────────────────────────────

class StubActionFactory;

class StubAction : public clang::ASTFrontendAction {
public:
    explicit StubAction(const StubConfig &cfg, StubActionFactory *factory = nullptr);

    std::unique_ptr<clang::ASTConsumer>
    CreateASTConsumer(clang::CompilerInstance &CI,
                      llvm::StringRef InFile) override;

    void EndSourceFileAction() override;

    unsigned getStubCount() const { return stub_count_; }
    unsigned getSkipCount() const { return skip_count_; }

private:
    clang::Rewriter rewriter_;
    const StubConfig &config_;
    StubActionFactory *factory_ = nullptr;
    unsigned stub_count_ = 0;
    unsigned skip_count_ = 0;
};

// ──────────────────────────────────────────────────────────
// Factory for ClangTool
// ──────────────────────────────────────────────────────────

class StubActionFactory : public clang::tooling::FrontendActionFactory {
public:
    explicit StubActionFactory(const StubConfig &cfg);
    std::unique_ptr<clang::FrontendAction> create() override;

    unsigned getTotalStubs() const { return total_stubs_; }
    unsigned getTotalSkips() const { return total_skips_; }

    void addCounts(unsigned stubs, unsigned skips);

private:
    const StubConfig &config_;
    unsigned total_stubs_ = 0;
    unsigned total_skips_ = 0;
};

} // namespace cppstubber

#endif // CPPSTUBBER_STUBBER_HPP
