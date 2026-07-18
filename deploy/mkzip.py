import zipfile, os, pathlib
base = pathlib.Path("/tmp/lambda-pkg")
dest = "/deploy/ep-deploy.zip"
with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
    for f in base.rglob("*"):
        if f.is_file() and "__pycache__" not in str(f) and ".pyc" not in str(f):
            z.write(f, f.relative_to(base))
print("zip size:", os.path.getsize(dest) // 1024, "KB")
