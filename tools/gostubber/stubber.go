package main

import (
	"bytes"
	"fmt"
	"go/ast"
	"go/format"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"strings"
)

const stubMarker = `"STUB: not implemented"`

type StubResult struct {
	FilesStubbed     int          `json:"stubbed_files"`
	FunctionsStubbed int          `json:"stubbed_functions"`
	FunctionsSkipped int          `json:"skipped_functions"`
	Files            []FileResult `json:"files"`
}

type FileResult struct {
	Path    string `json:"path"`
	Stubbed int    `json:"stubbed"`
	Skipped int    `json:"skipped"`
}

type Stubber struct {
	SkipTests  bool
	SkipVendor bool
	KeepDocs   bool
}

// stripComments removes PROSE comments (the per-function docs that leak the
// answer) while PRESERVING Go's semantically-significant comments, which are
// compile-critical and vary across libs old→new:
//   - build constraints: `//go:build ...` (new) and `// +build ...` (old)
//   - compiler/tool directives: `//go:embed`, `//go:linkname`, `//go:noinline`,
//     `//go:generate`, `//line ...`, `//nolint:...`, etc.
//   - the cgo preamble (the comment block immediately above `import "C"`, which
//     contains C code)
// Stripping these breaks the build (wrong platform gating, broken //go:embed,
// broken cgo). We filter LINE-BY-LINE so a mixed group ("// Foo does X" +
// "//go:noinline") keeps only the directive line.
func stripComments(node *ast.File) {
	cgoDoc := cgoPreamble(node)

	var kept []*ast.CommentGroup
	keptSet := map[*ast.CommentGroup]bool{}
	for _, cg := range node.Comments {
		if cg == cgoDoc {
			kept = append(kept, cg)
			keptSet[cg] = true
			continue
		}
		var lines []*ast.Comment
		for _, c := range cg.List {
			if lineIsDirective(c.Text) {
				lines = append(lines, c)
			}
		}
		if len(lines) > 0 {
			cg.List = lines
			kept = append(kept, cg)
			keptSet[cg] = true
		}
	}
	node.Comments = kept

	// Drop .Doc/.Comment references to stripped groups (else the printer resurrects
	// them); keep references to preserved directive/cgo groups.
	keep := func(cg *ast.CommentGroup) *ast.CommentGroup {
		if cg != nil && keptSet[cg] {
			return cg
		}
		return nil
	}
	node.Doc = keep(node.Doc)
	ast.Inspect(node, func(n ast.Node) bool {
		switch d := n.(type) {
		case *ast.FuncDecl:
			d.Doc = keep(d.Doc)
		case *ast.GenDecl:
			d.Doc = keep(d.Doc)
		case *ast.Field:
			d.Doc = keep(d.Doc)
			d.Comment = keep(d.Comment)
		case *ast.TypeSpec:
			d.Doc = keep(d.Doc)
			d.Comment = keep(d.Comment)
		case *ast.ValueSpec:
			d.Doc = keep(d.Doc)
			d.Comment = keep(d.Comment)
		case *ast.ImportSpec:
			d.Doc = keep(d.Doc)
			d.Comment = keep(d.Comment)
		}
		return true
	})
}

// cgoPreamble returns the C-preamble comment group immediately above `import "C"`
// (it contains C code and MUST survive), or nil.
func cgoPreamble(node *ast.File) *ast.CommentGroup {
	for _, d := range node.Decls {
		gd, ok := d.(*ast.GenDecl)
		if !ok || gd.Tok != token.IMPORT {
			continue
		}
		for _, spec := range gd.Specs {
			imp, ok := spec.(*ast.ImportSpec)
			if !ok || imp.Path == nil || imp.Path.Value != `"C"` {
				continue
			}
			if imp.Doc != nil {
				return imp.Doc
			}
			if gd.Doc != nil {
				return gd.Doc
			}
		}
	}
	return nil
}

// lineIsDirective reports whether a single comment line (raw, incl. "//") is a Go
// directive that affects compilation — go/ast's internal isDirective heuristic
// (`//word:...` with no space) plus the old `// +build` and `//line` forms.
func lineIsDirective(raw string) bool {
	if !strings.HasPrefix(raw, "//") {
		return false // block comments survive only via the cgo path
	}
	c := raw[2:]
	if strings.HasPrefix(strings.TrimLeft(c, " \t"), "+build") {
		return true // old-style build constraint "// +build ..."
	}
	if strings.HasPrefix(c, "line ") {
		return true // "//line file:line" directive
	}
	colon := strings.Index(c, ":")
	if colon <= 0 {
		return false
	}
	for i := 0; i < colon; i++ {
		b := c[i]
		if !((b >= 'a' && b <= 'z') || (b >= '0' && b <= '9')) {
			return false
		}
	}
	return true
}

func (s *Stubber) StubDirectory(dir string) (*StubResult, error) {
	result := &StubResult{}

	err := filepath.Walk(dir, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}

		if info.IsDir() {
			name := info.Name()
			if name == ".git" || name == "testdata" {
				return filepath.SkipDir
			}
			if s.SkipVendor && name == "vendor" {
				return filepath.SkipDir
			}
			return nil
		}

		if !strings.HasSuffix(path, ".go") {
			return nil
		}
		if s.SkipTests && strings.HasSuffix(path, "_test.go") {
			return nil
		}
		if isDocFile(info.Name()) {
			return nil
		}

		fileResult, err := s.stubFile(path)
		if err != nil {
			fmt.Fprintf(os.Stderr, "warning: skipping %s: %v\n", path, err)
			return nil
		}

		if fileResult.Stubbed > 0 {
			result.FilesStubbed++
		}
		result.FunctionsStubbed += fileResult.Stubbed
		result.FunctionsSkipped += fileResult.Skipped
		result.Files = append(result.Files, *fileResult)

		return nil
	})

	return result, err
}

func (s *Stubber) StubFile(path string) (*FileResult, error) {
	return s.stubFile(path)
}

func (s *Stubber) stubFile(path string) (*FileResult, error) {
	fset := token.NewFileSet()
	node, err := parser.ParseFile(fset, path, nil, parser.ParseComments)
	if err != nil {
		return nil, fmt.Errorf("parse error: %w", err)
	}

	result := &FileResult{Path: path}
	modified := false

	// Strip comments up front (unless --keep-docs): removes the answer-leaking
	// function docs AND prevents go/format from relocating a floating comment into
	// a rewritten body. Rewrite the file if anything was stripped, even when no
	// function was stubbable (a doc-only/type-only file still leaks via type docs).
	if !s.KeepDocs && (len(node.Comments) > 0 || node.Doc != nil) {
		stripComments(node)
		modified = true
	}

	for _, decl := range node.Decls {
		fn, ok := decl.(*ast.FuncDecl)
		if !ok {
			continue
		}

		if fn.Body == nil {
			continue
		}

		if isInitOrMain(fn) {
			result.Skipped++
			continue
		}

		if isAlreadyStubbed(fn.Body) {
			result.Skipped++
			continue
		}

		newBody := buildStubBody(fn)
		if newBody != nil {
			fn.Body = newBody
			result.Stubbed++
			modified = true
		}
	}

	if modified {
		var buf bytes.Buffer
		if err := format.Node(&buf, fset, node); err != nil {
			return nil, fmt.Errorf("format error: %w", err)
		}
		if err := os.WriteFile(path, buf.Bytes(), 0644); err != nil {
			return nil, fmt.Errorf("write error: %w", err)
		}
	}

	return result, nil
}

func isInitOrMain(fn *ast.FuncDecl) bool {
	name := fn.Name.Name
	return name == "init" || name == "main"
}

func isAlreadyStubbed(body *ast.BlockStmt) bool {
	for _, stmt := range body.List {
		assign, ok := stmt.(*ast.AssignStmt)
		if !ok {
			continue
		}
		for _, rhs := range assign.Rhs {
			lit, ok := rhs.(*ast.BasicLit)
			if ok && lit.Kind == token.STRING && lit.Value == stubMarker {
				return true
			}
		}
	}
	return false
}

func buildStubBody(fn *ast.FuncDecl) *ast.BlockStmt {
	stmts := []ast.Stmt{}

	markerStmt := &ast.AssignStmt{
		Lhs: []ast.Expr{&ast.Ident{Name: "_"}},
		Tok: token.ASSIGN,
		Rhs: []ast.Expr{&ast.BasicLit{
			Kind:  token.STRING,
			Value: stubMarker,
		}},
	}
	stmts = append(stmts, markerStmt)

	if fn.Type.Results == nil || len(fn.Type.Results.List) == 0 {
		stmts = append(stmts, &ast.ReturnStmt{})
		return &ast.BlockStmt{List: stmts}
	}

	var returnExprs []ast.Expr
	for _, field := range fn.Type.Results.List {
		zeroVal := zeroValueExpr(field.Type)
		names := len(field.Names)
		if names == 0 {
			names = 1
		}
		for i := 0; i < names; i++ {
			returnExprs = append(returnExprs, zeroVal)
		}
	}

	stmts = append(stmts, &ast.ReturnStmt{Results: returnExprs})
	return &ast.BlockStmt{List: stmts}
}

func derefNew(t ast.Expr) ast.Expr {
	return &ast.StarExpr{
		X: &ast.CallExpr{
			Fun:  &ast.Ident{Name: "new"},
			Args: []ast.Expr{t},
		},
	}
}

func zeroValueExpr(expr ast.Expr) ast.Expr {
	switch t := expr.(type) {
	case *ast.Ident:
		switch t.Name {
		case "bool":
			return &ast.Ident{Name: "false"}
		case "string":
			return &ast.BasicLit{Kind: token.STRING, Value: `""`}
		case "int", "int8", "int16", "int32", "int64",
			"uint", "uint8", "uint16", "uint32", "uint64",
			"float32", "float64", "complex64", "complex128",
			"byte", "rune", "uintptr":
			return &ast.BasicLit{Kind: token.INT, Value: "0"}
		case "error":
			return &ast.Ident{Name: "nil"}
		default:
			return derefNew(t)
		}
	case *ast.StarExpr:
		return &ast.Ident{Name: "nil"}
	case *ast.ArrayType:
		return &ast.Ident{Name: "nil"}
	case *ast.SliceExpr:
		return &ast.Ident{Name: "nil"}
	case *ast.MapType:
		return &ast.Ident{Name: "nil"}
	case *ast.ChanType:
		return &ast.Ident{Name: "nil"}
	case *ast.FuncType:
		return &ast.Ident{Name: "nil"}
	case *ast.InterfaceType:
		return &ast.Ident{Name: "nil"}
	case *ast.SelectorExpr:
		return derefNew(t)
	case *ast.IndexExpr:
		return &ast.Ident{Name: "nil"}
	case *ast.IndexListExpr:
		return &ast.Ident{Name: "nil"}
	default:
		return &ast.Ident{Name: "nil"}
	}
}
