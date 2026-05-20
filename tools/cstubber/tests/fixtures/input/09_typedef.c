typedef struct {
    int x;
    int y;
} Point;

Point point_add(Point a, Point b) {
    Point r = { a.x + b.x, a.y + b.y };
    return r;
}
