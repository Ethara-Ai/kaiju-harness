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

// stripComments removes ALL doc/ordinary comments from the file so the stubbed
// output can't leak per-function documentation (and so go/format can't
// misattribute a floating comment into a modified body). Clearing node.Comments
// alone isn't enough — the printer also emits each node's own .Doc/.Comment, so
// those are nil'd too.
func stripComments(node *ast.File) {
	node.Comments = nil
	node.Doc = nil
	ast.Inspect(node, func(n ast.Node) bool {
		switch d := n.(type) {
		case *ast.FuncDecl:
			d.Doc = nil
		case *ast.GenDecl:
			d.Doc = nil
		case *ast.Field:
			d.Doc = nil
			d.Comment = nil
		case *ast.TypeSpec:
			d.Doc = nil
			d.Comment = nil
		case *ast.ValueSpec:
			d.Doc = nil
			d.Comment = nil
		case *ast.ImportSpec:
			d.Doc = nil
			d.Comment = nil
		}
		return true
	})
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
