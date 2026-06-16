/**
 * Babel-based two-pass JavaScript stubber for commit0. Refuses .ts/.tsx.
 * Idempotent: re-running on stubbed source produces byte-identical output.
 *
 * Usage: node stub_js.ts --src-dir X [--extra-scan-dirs Y,Z] [--mode all] [--verbose]
 */

import * as fs from "node:fs";
import * as path from "node:path";

import { parse } from "@babel/parser";
import type { ParserOptions, ParserPlugin } from "@babel/parser";
import traverse from "@babel/traverse";
import type { NodePath } from "@babel/traverse";
import * as t from "@babel/types";

interface StubReport {
  files_processed: number;
  files_modified: number;
  files_skipped: number;
  functions_stubbed: number;
  functions_skipped_import_time: number;
  functions_skipped_other: number;
  import_time_names: string[];
  errors: { file: string; error: string }[];
}

interface CliArgs {
  srcDir: string;
  extraScanDirs: string[];
  mode: "all";
  verbose: boolean;
}

interface FuncTarget {
  bodyStart: number;
  bodyEnd: number;
  isArrowExpressionBody: boolean;
  name: string;
}

type FuncNode =
  | t.FunctionDeclaration
  | t.FunctionExpression
  | t.ArrowFunctionExpression
  | t.ClassMethod
  | t.ClassPrivateMethod
  | t.ObjectMethod;

type FuncPath = NodePath<FuncNode>;

const STUB_MARKER = "// __COMMIT0_STUB__";

function buildStubBody(indent: string): string {
  return `{\n${indent}  ${STUB_MARKER}\n${indent}  throw new Error("STUB");\n${indent}}`;
}

const JS_EXTENSIONS = new Set([".js", ".mjs", ".cjs", ".jsx"]);
const REFUSED_TS_EXTENSIONS = new Set([".ts", ".tsx", ".mts", ".cts"]);

const SKIP_DIR_NAMES = new Set([
  "node_modules",
  ".git",
  "dist",
  "build",
  "coverage",
  ".next",
  ".nuxt",
  ".turbo",
  ".cache",
  ".pnp",
  ".yarn",
  "out",
  ".nyc_output",
]);

const TEST_FILE_PATTERNS: RegExp[] = [
  /\.test\.[mc]?jsx?$/i,
  /\.spec\.[mc]?jsx?$/i,
  /[\\/]__tests__[\\/]/,
  /[\\/]__mocks__[\\/]/,
  /[\\/]tests?[\\/]/,
  /[\\/]fixtures[\\/]/,
  /\.stories\.[mc]?jsx?$/i,
];

const BUILTINS_TO_IGNORE = new Set([
  "console", "Math", "JSON", "Object", "Array", "String", "Number", "Boolean",
  "Promise", "Map", "Set", "WeakMap", "WeakSet", "Error", "TypeError",
  "RangeError", "Date", "RegExp", "Symbol", "parseInt", "parseFloat", "isNaN",
  "isFinite", "undefined", "NaN", "Infinity", "require", "module", "exports",
  "process", "Buffer", "setTimeout", "setInterval", "clearTimeout",
  "clearInterval", "queueMicrotask", "globalThis", "import",
]);

const PROTOCOL_METHOD_NAMES = new Set([
  "toString",
  "valueOf",
  "toJSON",
  "[Symbol.toPrimitive]",
  "[Symbol.iterator]",
  "[Symbol.asyncIterator]",
  "[Symbol.hasInstance]",
  "[Symbol.toStringTag]",
]);

const PARSER_PLUGINS: ParserPlugin[] = [
  "jsx",
  "classProperties",
  "classPrivateProperties",
  "classPrivateMethods",
  "classStaticBlock",
  "decorators-legacy",
  "exportDefaultFrom",
  "exportNamespaceFrom",
  "asyncGenerators",
  "objectRestSpread",
  "optionalChaining",
  "nullishCoalescingOperator",
  "dynamicImport",
  "topLevelAwait",
  "importMeta",
  "importAttributes",
  "numericSeparator",
  "logicalAssignment",
];

const MAX_TRANSITIVE_ITERATIONS = 10;

function logStderr(msg: string): void {
  process.stderr.write(msg + "\n");
}

function logVerbose(verbose: boolean, msg: string): void {
  if (verbose) logStderr(msg);
}

function parseArgs(argv: string[]): CliArgs {
  const args: CliArgs = {
    srcDir: "",
    extraScanDirs: [],
    mode: "all",
    verbose: false,
  };

  for (let i = 2; i < argv.length; i++) {
    const flag = argv[i];
    if (flag === "--src-dir") {
      args.srcDir = argv[++i] ?? "";
    } else if (flag === "--extra-scan-dirs") {
      const v = argv[++i] ?? "";
      args.extraScanDirs = v
        .split(",")
        .map((d) => d.trim())
        .filter((d) => d.length > 0);
    } else if (flag === "--mode") {
      const v = argv[++i] ?? "all";
      if (v !== "all") {
        logStderr(`Warning: --mode=${v} unsupported for JS stubber; using 'all'`);
      }
      args.mode = "all";
    } else if (flag === "--verbose") {
      args.verbose = true;
    } else {
      logStderr(`Unknown argument: ${flag}`);
    }
  }

  if (!args.srcDir) {
    logStderr("Error: --src-dir is required");
    process.exit(1);
  }

  return args;
}

function isTestFile(filePath: string): boolean {
  return TEST_FILE_PATTERNS.some((p) => p.test(filePath));
}

function isJsSource(filePath: string): boolean {
  const ext = path.extname(filePath).toLowerCase();
  return JS_EXTENSIONS.has(ext);
}

function isRefusedTs(filePath: string): boolean {
  const ext = path.extname(filePath).toLowerCase();
  return REFUSED_TS_EXTENSIONS.has(ext);
}

function walkJsFiles(rootAbs: string): string[] {
  const out: string[] = [];
  const stack: string[] = [rootAbs];
  while (stack.length > 0) {
    const cur = stack.pop();
    if (cur === undefined) break;
    let entries: fs.Dirent[];
    try {
      entries = fs.readdirSync(cur, { withFileTypes: true });
    } catch (_e) {
      continue;
    }
    for (const ent of entries) {
      if (ent.name.startsWith(".") && ent.name !== "." && ent.name !== "..") {
        continue;
      }
      if (SKIP_DIR_NAMES.has(ent.name)) continue;
      const full = path.join(cur, ent.name);
      if (ent.isDirectory()) {
        stack.push(full);
      } else if (ent.isFile() && isJsSource(full)) {
        out.push(full);
      }
    }
  }
  out.sort();
  return out;
}

function parseSource(filePath: string, source: string): t.File {
  const ast = parse(source, {
    sourceType: "unambiguous",
    sourceFilename: filePath,
    allowImportExportEverywhere: true,
    allowReturnOutsideFunction: true,
    allowAwaitOutsideFunction: true,
    errorRecovery: true,
    plugins: PARSER_PLUGINS,
  });
  if (ast.errors && ast.errors.length > 0) {
    const first = ast.errors[0];
    const msg = first instanceof Error ? first.message : String(first);
    throw new Error(
      `recovered ${ast.errors.length} parse error(s); first: ${msg}`,
    );
  }
  return ast;
}

function calleeName(callee: t.Node): string | null {
  if (t.isIdentifier(callee)) return callee.name;
  if (t.isMemberExpression(callee)) {
    const prop = callee.property;
    if (!callee.computed && t.isIdentifier(prop)) return prop.name;
  }
  if (t.isV8IntrinsicIdentifier(callee)) return callee.name;
  return null;
}

function functionDisplayName(node: FuncNode, parent: t.Node | null): string {
  if (t.isFunctionDeclaration(node) || t.isFunctionExpression(node)) {
    if (node.id && t.isIdentifier(node.id)) return node.id.name;
  }
  if (t.isClassMethod(node) || t.isObjectMethod(node)) {
    const key = node.key;
    if (t.isIdentifier(key)) return key.name;
    if (t.isStringLiteral(key)) return key.value;
    if (t.isPrivateName(key) && t.isIdentifier(key.id)) return `#${key.id.name}`;
  }
  if (t.isClassPrivateMethod(node)) {
    const key = node.key;
    if (t.isPrivateName(key) && t.isIdentifier(key.id)) return `#${key.id.name}`;
  }
  if (parent !== null) {
    if (t.isVariableDeclarator(parent) && t.isIdentifier(parent.id)) {
      return parent.id.name;
    }
    if (t.isAssignmentExpression(parent)) {
      if (t.isIdentifier(parent.left)) return parent.left.name;
      if (t.isMemberExpression(parent.left)) {
        const prop = parent.left.property;
        if (!parent.left.computed && t.isIdentifier(prop)) return prop.name;
      }
    }
    if (t.isProperty(parent) || t.isObjectProperty(parent)) {
      const key = (parent as t.ObjectProperty).key;
      if (t.isIdentifier(key)) return key.name;
      if (t.isStringLiteral(key)) return key.value;
    }
  }
  return "(anonymous)";
}

function methodKeyName(node: t.ClassMethod | t.ObjectMethod | t.ClassPrivateMethod): string {
  if (t.isClassPrivateMethod(node)) {
    const key = node.key;
    if (t.isPrivateName(key) && t.isIdentifier(key.id)) return `#${key.id.name}`;
    return "(private)";
  }
  const key = (node as t.ClassMethod | t.ObjectMethod).key;
  if (t.isIdentifier(key)) return key.name;
  if (t.isStringLiteral(key)) return key.value;
  if (t.isMemberExpression(key)) {
    const obj = key.object;
    const prop = key.property;
    if (
      t.isIdentifier(obj) &&
      obj.name === "Symbol" &&
      t.isIdentifier(prop)
    ) {
      return `[Symbol.${prop.name}]`;
    }
  }
  return "(computed)";
}

function collectCallsIn(node: t.Node, out: Set<string>): void {
  t.traverseFast(node, (n: t.Node) => {
    if (t.isCallExpression(n) || t.isNewExpression(n)) {
      const name = calleeName(n.callee);
      if (name !== null && !BUILTINS_TO_IGNORE.has(name)) out.add(name);
      return;
    }
    if (t.isTaggedTemplateExpression(n)) {
      const name = calleeName(n.tag);
      if (name !== null && !BUILTINS_TO_IGNORE.has(name)) out.add(name);
    }
  });
}

function collectImportTimeNamesFromFile(ast: t.File, into: Set<string>): void {
  const body = ast.program.body;
  for (const stmt of body) {
    if (t.isExpressionStatement(stmt)) {
      collectCallsIn(stmt.expression, into);
      continue;
    }
    if (t.isVariableDeclaration(stmt)) {
      for (const decl of stmt.declarations) {
        if (decl.init !== null && decl.init !== undefined) {
          collectCallsIn(decl.init, into);
        }
      }
      continue;
    }
    if (t.isIfStatement(stmt) || t.isTryStatement(stmt) || t.isSwitchStatement(stmt)) {
      collectCallsIn(stmt, into);
      continue;
    }
    if (t.isClassDeclaration(stmt)) {
      collectClassImportTime(stmt, into);
      continue;
    }
    if (t.isExportNamedDeclaration(stmt)) {
      if (stmt.declaration !== null && stmt.declaration !== undefined) {
        if (t.isClassDeclaration(stmt.declaration)) {
          collectClassImportTime(stmt.declaration, into);
        } else if (t.isVariableDeclaration(stmt.declaration)) {
          for (const decl of stmt.declaration.declarations) {
            if (decl.init !== null && decl.init !== undefined) {
              collectCallsIn(decl.init, into);
            }
          }
        }
      }
      if (stmt.source === null || stmt.source === undefined) {
        for (const spec of stmt.specifiers) {
          if (t.isExportSpecifier(spec) && t.isIdentifier(spec.local)) {
            into.add(spec.local.name);
          }
        }
      }
      continue;
    }
    if (t.isExportDefaultDeclaration(stmt)) {
      if (
        !t.isFunctionDeclaration(stmt.declaration) &&
        !t.isClassDeclaration(stmt.declaration)
      ) {
        collectCallsIn(stmt.declaration, into);
      }
      if (t.isClassDeclaration(stmt.declaration)) {
        collectClassImportTime(stmt.declaration, into);
      }
      continue;
    }
    if (t.isLabeledStatement(stmt)) {
      collectCallsIn(stmt.body, into);
      continue;
    }
  }
  for (const b of BUILTINS_TO_IGNORE) into.delete(b);
}

function collectClassImportTime(cls: t.ClassDeclaration, into: Set<string>): void {
  for (const dec of cls.decorators ?? []) {
    collectCallsIn(dec.expression, into);
  }
  for (const member of cls.body.body) {
    for (const dec of (member as { decorators?: t.Decorator[] }).decorators ?? []) {
      collectCallsIn(dec.expression, into);
    }
    if (t.isClassProperty(member) && member.static === true) {
      if (member.value !== null && member.value !== undefined) {
        collectCallsIn(member.value, into);
      }
    }
    if (t.isStaticBlock(member)) {
      for (const stmt of member.body) {
        collectCallsIn(stmt, into);
      }
    }
  }
}

interface CallGraph {
  callees: Map<string, Set<string>>;
  defined: Set<string>;
}

function buildCallGraphFromAst(ast: t.File, graph: CallGraph): void {
  const addCallees = (name: string, body: t.Node | null | undefined): void => {
    if (body === null || body === undefined) return;
    graph.defined.add(name);
    const calls = new Set<string>();
    collectCallsIn(body, calls);
    const existing = graph.callees.get(name);
    if (existing === undefined) {
      graph.callees.set(name, calls);
    } else {
      for (const c of calls) existing.add(c);
    }
  };

  traverse(ast, {
    FunctionDeclaration(p: NodePath<t.FunctionDeclaration>): void {
      const id = p.node.id;
      if (id !== null && id !== undefined) addCallees(id.name, p.node.body);
    },
    FunctionExpression(p: NodePath<t.FunctionExpression>): void {
      const id = p.node.id;
      const parent = p.parent;
      let name: string | null = null;
      if (id !== null && id !== undefined) {
        name = id.name;
      } else if (t.isVariableDeclarator(parent) && t.isIdentifier(parent.id)) {
        name = parent.id.name;
      }
      if (name !== null) addCallees(name, p.node.body);
    },
    ArrowFunctionExpression(p: NodePath<t.ArrowFunctionExpression>): void {
      const parent = p.parent;
      if (t.isVariableDeclarator(parent) && t.isIdentifier(parent.id)) {
        addCallees(parent.id.name, p.node.body);
      }
    },
    ClassMethod(p: NodePath<t.ClassMethod>): void {
      addCallees(methodKeyName(p.node), p.node.body);
    },
    ClassPrivateMethod(p: NodePath<t.ClassPrivateMethod>): void {
      addCallees(methodKeyName(p.node), p.node.body);
    },
    ObjectMethod(p: NodePath<t.ObjectMethod>): void {
      addCallees(methodKeyName(p.node), p.node.body);
    },
  });
}

function resolveTransitive(
  seed: Set<string>,
  graph: CallGraph,
  verbose: boolean,
): Set<string> {
  const resolved = new Set(seed);
  for (let iter = 0; iter < MAX_TRANSITIVE_ITERATIONS; iter++) {
    const before = resolved.size;
    const fresh = new Set<string>();
    for (const name of resolved) {
      const calls = graph.callees.get(name);
      if (calls === undefined) continue;
      for (const callee of calls) {
        if (
          !resolved.has(callee) &&
          graph.defined.has(callee) &&
          !BUILTINS_TO_IGNORE.has(callee)
        ) {
          fresh.add(callee);
        }
      }
    }
    for (const n of fresh) resolved.add(n);
    if (fresh.size > 0) {
      logVerbose(
        verbose,
        `  Transitive iter ${iter}: +${fresh.size} ` +
          `(${[...fresh].slice(0, 8).join(", ")}${fresh.size > 8 ? ", ..." : ""})`,
      );
    }
    if (resolved.size === before) break;
  }
  return resolved;
}

function isAlreadyStubbedSource(bodyText: string): boolean {
  return (
    bodyText.includes(STUB_MARKER) && bodyText.includes('throw new Error("STUB")')
  );
}

function isEmptyBlock(body: t.Node | null | undefined): boolean {
  if (body === null || body === undefined) return false;
  if (t.isBlockStatement(body) && body.body.length === 0) return true;
  return false;
}

function isSimpleConstructor(node: t.ClassMethod): boolean {
  if (node.kind !== "constructor") return false;
  const body = node.body.body;
  for (const stmt of body) {
    if (!t.isExpressionStatement(stmt)) return false;
    const e = stmt.expression;
    if (t.isCallExpression(e) && t.isSuper(e.callee)) continue;
    if (
      t.isAssignmentExpression(e) &&
      t.isMemberExpression(e.left) &&
      t.isThisExpression(e.left.object)
    ) {
      continue;
    }
    return false;
  }
  return true;
}

function isAbstractClassMethod(node: t.ClassMethod | t.ClassPrivateMethod): boolean {
  return (node as { abstract?: boolean }).abstract === true;
}

function isProtocolMethodName(name: string): boolean {
  return PROTOCOL_METHOD_NAMES.has(name);
}

function indentationFor(source: string, offset: number): string {
  let lineStart = offset;
  while (lineStart > 0 && source.charCodeAt(lineStart - 1) !== 10) {
    lineStart--;
  }
  let i = lineStart;
  while (i < offset && (source[i] === " " || source[i] === "\t")) i++;
  return source.slice(lineStart, i);
}

function buildTargetsForFile(
  ast: t.File,
  source: string,
  importTime: Set<string>,
  report: StubReport,
  filePath: string,
  verbose: boolean,
): FuncTarget[] {
  const candidates: { path: FuncPath; body: t.Node; name: string }[] = [];

  const considerBody = (
    p: FuncPath,
    body: t.Node | null | undefined,
    name: string,
  ): void => {
    if (body === null || body === undefined) return;
    candidates.push({ path: p, body, name });
  };

  traverse(ast, {
    FunctionDeclaration(p: NodePath<t.FunctionDeclaration>): void {
      const name = functionDisplayName(p.node, p.parent);
      considerBody(p, p.node.body, name);
    },
    FunctionExpression(p: NodePath<t.FunctionExpression>): void {
      const name = functionDisplayName(p.node, p.parent);
      considerBody(p, p.node.body, name);
    },
    ArrowFunctionExpression(p: NodePath<t.ArrowFunctionExpression>): void {
      const name = functionDisplayName(p.node, p.parent);
      considerBody(p, p.node.body, name);
    },
    ClassMethod(p: NodePath<t.ClassMethod>): void {
      const name = methodKeyName(p.node);
      considerBody(p, p.node.body, name);
    },
    ClassPrivateMethod(p: NodePath<t.ClassPrivateMethod>): void {
      const name = methodKeyName(p.node);
      considerBody(p, p.node.body, name);
    },
    ObjectMethod(p: NodePath<t.ObjectMethod>): void {
      const name = methodKeyName(p.node);
      considerBody(p, p.node.body, name);
    },
  });

  const candidateNodes = new Set<t.Node>(candidates.map((c) => c.path.node));

  const targets: FuncTarget[] = [];
  for (const { path: p, body, name } of candidates) {
    const node = p.node;
    if (t.isClassMethod(node) && node.kind === "constructor") {
      if (isSimpleConstructor(node)) {
        report.functions_skipped_other++;
        continue;
      }
    }
    if (
      (t.isClassMethod(node) || t.isClassPrivateMethod(node)) &&
      isAbstractClassMethod(node)
    ) {
      report.functions_skipped_other++;
      continue;
    }
    if (isEmptyBlock(body)) {
      report.functions_skipped_other++;
      continue;
    }
    if (isProtocolMethodName(name)) {
      report.functions_skipped_other++;
      continue;
    }
    if (importTime.has(name)) {
      report.functions_skipped_import_time++;
      logVerbose(verbose, `  [SKIP-IMPORT] ${path.basename(filePath)}::${name}`);
      continue;
    }
    let ancestorIsCandidate = false;
    let parentPath: NodePath<t.Node> | null = p.parentPath;
    while (parentPath !== null) {
      if (candidateNodes.has(parentPath.node)) {
        ancestorIsCandidate = true;
        break;
      }
      parentPath = parentPath.parentPath;
    }
    if (ancestorIsCandidate) {
      report.functions_skipped_other++;
      continue;
    }
    const bodyStart = body.start;
    const bodyEnd = body.end;
    if (
      bodyStart === null ||
      bodyStart === undefined ||
      bodyEnd === null ||
      bodyEnd === undefined
    ) {
      report.errors.push({ file: filePath, error: `${name}: body has no location` });
      continue;
    }
    const bodyText = source.slice(bodyStart, bodyEnd);
    if (isAlreadyStubbedSource(bodyText)) {
      report.functions_skipped_other++;
      continue;
    }
    const isArrowExprBody = t.isArrowFunctionExpression(node) && !t.isBlockStatement(body);
    targets.push({
      bodyStart,
      bodyEnd,
      isArrowExpressionBody: isArrowExprBody,
      name,
    });
  }
  return targets;
}

function applyReplacements(source: string, targets: FuncTarget[]): string {
  targets.sort((a, b) => b.bodyStart - a.bodyStart);
  let out = source;
  for (const tgt of targets) {
    const indent = indentationFor(out, tgt.bodyStart);
    const body = buildStubBody(indent);
    out = out.slice(0, tgt.bodyStart) + body + out.slice(tgt.bodyEnd);
  }
  return out;
}

function processFile(
  filePath: string,
  source: string,
  importTime: Set<string>,
  report: StubReport,
  verbose: boolean,
): string | null {
  let ast: t.File;
  try {
    ast = parseSource(filePath, source);
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    report.errors.push({ file: filePath, error: `parse failed: ${msg}` });
    return null;
  }
  const targets = buildTargetsForFile(ast, source, importTime, report, filePath, verbose);
  if (targets.length === 0) return null;
  report.functions_stubbed += targets.length;
  for (const tgt of targets) {
    logVerbose(verbose, `  [STUB] ${path.basename(filePath)}::${tgt.name}`);
  }
  const transformed = applyReplacements(source, targets);
  // Re-parse without errorRecovery to catch any case where stubbing produced
  // syntactically invalid output. Mirrors Python tools/stub.py post-rewrite check.
  try {
    parse(transformed, {
      sourceType: "unambiguous",
      sourceFilename: filePath,
      allowImportExportEverywhere: true,
      allowReturnOutsideFunction: true,
      allowAwaitOutsideFunction: true,
      errorRecovery: false,
      plugins: PARSER_PLUGINS,
    });
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    report.errors.push({
      file: filePath,
      error: `post-stub re-parse failed (original kept): ${msg}`,
    });
    report.functions_stubbed -= targets.length;
    return null;
  }
  return transformed;
}

function main(): void {
  const args = parseArgs(process.argv);

  const srcDirAbs = path.resolve(args.srcDir);
  const extraDirsAbs = args.extraScanDirs.map((d) => path.resolve(d));

  logStderr(
    `stub_js: src-dir=${srcDirAbs}, extra-scan-dirs=[${extraDirsAbs.join(", ")}], mode=${args.mode}`,
  );

  const report: StubReport = {
    files_processed: 0,
    files_modified: 0,
    files_skipped: 0,
    functions_stubbed: 0,
    functions_skipped_import_time: 0,
    functions_skipped_other: 0,
    import_time_names: [],
    errors: [],
  };

  if (!fs.existsSync(srcDirAbs) || !fs.statSync(srcDirAbs).isDirectory()) {
    logStderr(`Error: src-dir does not exist or is not a directory: ${srcDirAbs}`);
    process.stdout.write(JSON.stringify(report, null, 2) + "\n");
    process.exit(1);
  }

  const srcFiles = walkJsFiles(srcDirAbs);
  const extraFiles: string[] = [];
  for (const dir of extraDirsAbs) {
    if (!fs.existsSync(dir)) continue;
    for (const f of walkJsFiles(dir)) {
      if (!srcFiles.includes(f)) extraFiles.push(f);
    }
  }

  const refusedTs: string[] = [];
  const checkRefuse = (rootAbs: string): void => {
    const stack: string[] = [rootAbs];
    while (stack.length > 0) {
      const cur = stack.pop();
      if (cur === undefined) break;
      let entries: fs.Dirent[];
      try {
        entries = fs.readdirSync(cur, { withFileTypes: true });
      } catch (_e) {
        continue;
      }
      for (const ent of entries) {
        if (SKIP_DIR_NAMES.has(ent.name) || ent.name.startsWith(".")) continue;
        const full = path.join(cur, ent.name);
        if (ent.isDirectory()) {
          stack.push(full);
        } else if (ent.isFile() && isRefusedTs(full)) {
          refusedTs.push(full);
          if (refusedTs.length > 4) return;
        }
      }
    }
  };
  checkRefuse(srcDirAbs);
  if (refusedTs.length > 0) {
    logStderr(
      `Error: stub_js refuses TypeScript files (found ${refusedTs.length}; use stub_ts.ts). ` +
        `First match: ${refusedTs[0]}`,
    );
    process.stdout.write(JSON.stringify(report, null, 2) + "\n");
    process.exit(2);
  }

  logStderr(`Loaded ${srcFiles.length} src files + ${extraFiles.length} extra-scan files`);

  logStderr("Pass 1: collecting import-time names...");
  const rawImportTime = new Set<string>();
  const graph: CallGraph = { callees: new Map(), defined: new Set() };

  const allScanFiles = [...srcFiles, ...extraFiles];
  for (const f of allScanFiles) {
    if (isTestFile(f)) continue;
    let source: string;
    try {
      source = fs.readFileSync(f, "utf-8");
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      report.errors.push({ file: f, error: `read failed: ${msg}` });
      continue;
    }
    let ast: t.File;
    try {
      ast = parseSource(f, source);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      report.errors.push({ file: f, error: `parse failed in pass 1: ${msg}` });
      continue;
    }
    collectImportTimeNamesFromFile(ast, rawImportTime);
    buildCallGraphFromAst(ast, graph);
  }

  logVerbose(
    args.verbose,
    `  Raw import-time names (${rawImportTime.size}): ` +
      `${[...rawImportTime].slice(0, 16).join(", ")}`,
  );

  const importTime = resolveTransitive(rawImportTime, graph, args.verbose);
  logStderr(
    `Pass 1 complete: ${importTime.size} import-time names ` +
      `(${rawImportTime.size} direct + ${importTime.size - rawImportTime.size} transitive)`,
  );
  report.import_time_names = [...importTime].sort();

  logStderr("Pass 2: stubbing functions...");
  for (const f of srcFiles) {
    if (isTestFile(f)) {
      report.files_skipped++;
      continue;
    }
    report.files_processed++;
    let source: string;
    try {
      source = fs.readFileSync(f, "utf-8");
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      report.errors.push({ file: f, error: `read failed: ${msg}` });
      continue;
    }
    const transformed = processFile(f, source, importTime, report, args.verbose);
    if (transformed !== null && transformed !== source) {
      const tmp = `${f}.stubtmp.${process.pid}`;
      try {
        fs.writeFileSync(tmp, transformed, "utf-8");
        fs.renameSync(tmp, f);
        report.files_modified++;
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        report.errors.push({ file: f, error: `write failed: ${msg}` });
        fs.rmSync(tmp, { force: true });
      }
    }
  }

  logStderr(
    `Done. ${report.files_processed} processed, ${report.files_modified} modified, ` +
      `${report.functions_stubbed} stubbed, ${report.functions_skipped_import_time} ` +
      `import-time preserved, ${report.functions_skipped_other} other-skipped`,
  );
  if (report.errors.length > 0) {
    logStderr(`  ${report.errors.length} error(s) encountered`);
  }

  process.stdout.write(JSON.stringify(report, null, 2) + "\n");
}

main();
