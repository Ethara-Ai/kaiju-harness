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
