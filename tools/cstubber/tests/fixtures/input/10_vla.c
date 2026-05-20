int vla_sum(int n) {
    int buf[n];
    int total = 0;
    for (int i = 0; i < n; i++) {
        buf[i] = i;
        total += buf[i];
    }
    return total;
}
