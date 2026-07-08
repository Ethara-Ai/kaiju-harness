#include "stubber.hpp"

#include "clang/AST/Attr.h"
#include "clang/AST/DeclCXX.h"
#include "clang/AST/DeclTemplate.h"
#include "clang/Basic/SourceManager.h"
#include "clang/Lex/Lexer.h"

#include <algorithm>
#include <string>

namespace cppstubber {

static bool hasAttr(const clang::FunctionDecl *FD, llvm::StringRef name) {
    for (const auto *A : FD->attrs()) {
        if (A->getSpelling() == name)
            return true;
    }
    return false;
}

static bool isInTestFile(const clang::FunctionDecl *FD,
                         const clang::SourceManager &SM) {
    auto loc = FD->getLocation();
    if (loc.isInvalid())
        return false;
    auto fname = SM.getFilename(SM.getSpellingLoc(loc));
    if (fname.empty())
        return false;

    std::string lower = fname.lower();
    auto has = [&](const char *s) { return lower.find(s) != std::string::npos; };
    return has("test") || has("_test.") ||
           has("_tests.") || has("/tests/") ||
           has("/test/") || has("_unittest") ||
           has("_benchmark") || has("/bench/") ||
           has("/benchmarks/");
}

StubVisitor::StubVisitor(clang::Rewriter &R, const StubConfig &cfg,
                         clang::ASTContext &ctx)
    : rewriter_(R), config_(cfg), context_(ctx) {}

bool StubVisitor::shouldSkip(const clang::FunctionDecl *FD) const {
    if (!FD->hasBody())
        return true;

    if (!FD->isThisDeclarationADefinition())
        return true;

    if (FD->isImplicit())
        return true;

    auto &SM = context_.getSourceManager();
    if (!SM.isInMainFile(FD->getLocation()))
        return true;

    if (FD->isMain())
        return true;

    if (auto *CD = llvm::dyn_cast<clang::CXXDestructorDecl>(FD))
        return true;

    if (auto *MD = llvm::dyn_cast<clang::CXXMethodDecl>(FD)) {
        if (MD->isDefaulted() || MD->isDeleted())
            return true;

        if (MD->isPureVirtual())
            return true;

        if (!config_.stub_private && MD->getAccess() == clang::AS_private)
            return true;
    }

    if (auto *CD = llvm::dyn_cast<clang::CXXConstructorDecl>(FD)) {
        if (CD->isMoveConstructor())
            return true;
    }

    if (isTestRelated(FD))
        return true;

    if (isInTestFile(FD, context_.getSourceManager()))
        return true;

    return false;
}

bool StubVisitor::isTestRelated(const clang::FunctionDecl *FD) const {
    auto name = FD->getNameAsString();

    static const std::vector<std::string> prefixes = {
        "TEST",          "TEST_F",      "TEST_P",
        "TYPED_TEST",    "TYPED_TEST_P",
        "INSTANTIATE_TEST_SUITE_P",
        "TEST_CASE",     "SECTION",     "SCENARIO",
        "GIVEN",         "WHEN",        "THEN",
        "SUBCASE",       "DOCTEST_TEST_CASE",
        "BOOST_AUTO_TEST_CASE", "BOOST_FIXTURE_TEST_CASE",
        "BOOST_DATA_TEST_CASE",
    };

    for (const auto &p : prefixes) {
        if (name.find(p) == 0)
            return true;
    }

    if (hasAttr(FD, "test"))
        return true;

    return false;
}

std::string StubVisitor::chooseMarker(const clang::FunctionDecl *FD) const {
    if (FD->isConstexpr())
        return config_.constexpr_marker;

    auto *FPT = FD->getType()->getAs<clang::FunctionProtoType>();
    if (FPT && FPT->isNothrow())
        return config_.noexcept_marker;

    return config_.stub_marker;
}

bool StubVisitor::VisitFunctionDecl(clang::FunctionDecl *FD) {
    if (shouldSkip(FD)) {
        ++skip_count_;
        return true;
    }

    auto *body = FD->getBody();
    if (!body)
        return true;

    auto &SM = context_.getSourceManager();
    auto beginLoc = body->getBeginLoc();
    auto endLoc = body->getEndLoc();

    if (beginLoc.isInvalid() || endLoc.isInvalid())
        return true;

    if (beginLoc.isMacroID() || endLoc.isMacroID()) {
        ++skip_count_;
        return true;
    }

    std::string marker = chooseMarker(FD);
    std::string replacement = "{\n    " + marker + "\n}";

    auto range = clang::SourceRange(beginLoc, endLoc);
    rewriter_.ReplaceText(range, replacement);
    ++stub_count_;

    return true;
}

StubConsumer::StubConsumer(clang::Rewriter &R, const StubConfig &cfg,
                           clang::ASTContext &ctx)
    : visitor_(R, cfg, ctx) {}

void StubConsumer::HandleTranslationUnit(clang::ASTContext &ctx) {
    visitor_.TraverseDecl(ctx.getTranslationUnitDecl());
}

StubAction::StubAction(const StubConfig &cfg, StubActionFactory *factory)
    : config_(cfg), factory_(factory) {}

std::unique_ptr<clang::ASTConsumer>
StubAction::CreateASTConsumer(clang::CompilerInstance &CI,
                              llvm::StringRef InFile) {
    rewriter_.setSourceMgr(CI.getSourceManager(), CI.getLangOpts());
    return std::make_unique<StubConsumer>(rewriter_, config_,
                                         CI.getASTContext());
}

void StubAction::EndSourceFileAction() {
    auto &SM = rewriter_.getSourceMgr();
    auto mainID = SM.getMainFileID();

    auto *consumer = static_cast<StubConsumer *>(
        &getCompilerInstance().getASTConsumer());
    stub_count_ = consumer->getStubCount();
    skip_count_ = consumer->getSkipCount();

    if (factory_)
        factory_->addCounts(stub_count_, skip_count_);

    if (stub_count_ == 0)
        return;

    auto fileStart = SM.getLocForStartOfFile(mainID);
    auto buf = SM.getBufferData(mainID);
    bool has_stdexcept = buf.contains("<stdexcept>");
    bool has_cstdlib = buf.contains("<cstdlib>") || buf.contains("<stdlib.h>");

    std::string includes;
    if (!has_stdexcept)
        includes += "#include <stdexcept>\n";
    if (!has_cstdlib)
        includes += "#include <cstdlib>\n";
    if (!includes.empty())
        rewriter_.InsertTextBefore(fileStart, includes);

    if (config_.in_place) {
        rewriter_.overwriteChangedFiles();
    } else {
        rewriter_.getEditBuffer(mainID).write(llvm::outs());
    }
}

StubActionFactory::StubActionFactory(const StubConfig &cfg) : config_(cfg) {}

std::unique_ptr<clang::FrontendAction> StubActionFactory::create() {
    return std::make_unique<StubAction>(config_, this);
}

void StubActionFactory::addCounts(unsigned stubs, unsigned skips) {
    total_stubs_ += stubs;
    total_skips_ += skips;
}

} // namespace cppstubber
