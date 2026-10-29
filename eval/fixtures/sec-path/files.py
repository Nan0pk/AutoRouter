import os


def read_file(base, name):
    path = os.path.join(base, name)
    with open(path) as f:
        return f.read()
