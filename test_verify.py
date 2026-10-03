import main
print("FastAPI loaded successfully, routes count:", len(main.app.routes))
for r in main.app.routes:
    if "planificaciones" in getattr(r, 'path', ''):
        print(f"Route: {r.path} [{getattr(r, 'methods', None)}]")
