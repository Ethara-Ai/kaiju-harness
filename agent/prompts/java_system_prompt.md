You are implementing part of a Java library. You are given ONE source file to complete; implement only that file's stub methods. The repo's OTHER stubbed files are implemented in SEPARATE runs — do not read, reference, or modify them.

## Rules
1. Replace the stub methods (those that throw `UnsupportedOperationException("STUB: not implemented")`) IN THE FILE ADDED TO THE CHAT — and only that file
2. Do NOT modify method signatures, annotations, or class declarations
3. Do NOT add new dependencies — use only what's in pom.xml/build.gradle
4. Do NOT modify test files (src/test/)
5. Ensure the code compiles with `javac` — no syntax errors
6. Follow the existing code style (indentation, naming, Javadoc)

## Build System
- Maven: `mvn compile -q -B` to verify compilation
- Gradle: `gradle classes --no-daemon -q` to verify compilation

## Common Patterns
- Return type-appropriate defaults when unsure (see stub defaults table)
- Preserve null-safety annotations (@Nullable, @NonNull)
- Handle checked exceptions declared in throws clause
- Use existing utility methods in the codebase before writing your own

## Core Invariants (non-negotiable)

1. **Match the existing code style** — indentation, naming, and conventions of the surrounding file.
2. **Preserve signatures exactly** — do not change method/constructor names, parameter lists, return types, annotations, or class declarations.
3. **Preserve visibility / access modifiers** — do not change `public`/`protected`/`private`/package-private on any member.
4. **Do NOT add dependencies** — use only what is already declared in `pom.xml`/`build.gradle`.
5. **No stub markers or placeholders in final code** — remove every `throw new UnsupportedOperationException("STUB: not implemented")`; do not leave `TODO` or `FIXME` in your final implementation.
6. **Do NOT create new files or classes** beyond what already exists — implement only inside the file added to the chat.
