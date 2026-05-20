static int g_registered = 0;

__attribute__((constructor))
static void register_module(void) {
    g_registered = 1;
}
