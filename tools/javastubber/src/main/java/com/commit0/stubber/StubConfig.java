package com.commit0.stubber;

public class StubConfig {
    public boolean writeInPlace = false;
    // QC-C6-008: strip Javadoc from stubs by default. A retained Javadoc block can
    // embed the reference implementation ({@code ...}, <pre>...</pre>, @implSpec),
    // leaking the answer into the stubbed base. Stripping is the safe default,
    // consistent with the doc-stripping stubbers for Rust/Go.
    public boolean preserveJavadoc = false;
    public boolean stubPrivateMethods = false;
    public boolean stubConstructors = false;
    public String stubMarker = "throw new UnsupportedOperationException(\"STUB: not implemented\")";
    public String[] skipAnnotations = {"Deprecated"};
    public int maxFileLines = 50000;
}
