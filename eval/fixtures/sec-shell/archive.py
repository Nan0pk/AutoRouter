import subprocess


def extract(path, dest):
    subprocess.run(f"tar -xf {path} -C {dest}", shell=True, check=True)
