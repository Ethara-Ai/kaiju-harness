int compute(int n) {
    if (n > 0) {
        for (int i = 0; i < n; i++) {
            if (i % 2 == 0) {
                n += i;
            }
        }
    }
    return n;
}
