"""Paths shared by legacy source checkouts and Strata's terminal/engine layout."""

def terminal_directory(root):
    source = root / "source"
    return source / "terminal" if (source / "terminal" / "setup.py").is_file() else source


def engine_sources(terminal):
    engine = terminal.parent / "engine"
    return engine if terminal.name == "terminal" and (engine / "CMakeLists.txt").is_file() else terminal
