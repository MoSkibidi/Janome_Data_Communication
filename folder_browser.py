# folder_browser.py
# Lists and creates folders on the computer the panel runs on, so the settings page can offer a
# "Browse..." dialog for the CSV folder. A web page cannot read the real path of a folder from the
# browser's own file dialog, so the panel (which runs on this computer) lists the folders itself.
# Only folder names are listed, never files. The panel only serves this to the computer it runs on.
# English comments only

import os
import string
import sys

INVALID_NAME_CHARS = set('<>:"/\\|?*') | {chr(c) for c in range(32)}


class BrowseError(Exception):
    """A folder cannot be listed or created (message is meant for the user)."""


def roots():
    """Where browsing starts: the drive letters on Windows, '/' elsewhere."""
    if sys.platform == "win32" or os.name == "nt":
        return [f"{letter}:\\" for letter in string.ascii_uppercase if os.path.exists(f"{letter}:\\")]
    return ["/"]


def home():
    """The user's home folder (the Users folder of the signed-in user on Windows): where browsing starts."""
    return os.path.normpath(os.path.expanduser("~"))


def _absolute(path, base):
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        path = os.path.join(base, path)
    return os.path.normpath(os.path.abspath(path))


def browse(path, base):
    """
    Lists the sub-folders of `path` (relative paths are taken from `base`). An empty path starts at
    the user's home folder. Returns:
      {"path": the folder, "parent": its parent ("" when it is a drive or '/'), "home": the home folder,
       "roots": [...], "folders": [{"name", "path"}, ...]}
    """
    target = _absolute(path, base) if path else home()
    while not os.path.isdir(target):  # a folder that does not exist yet: show the closest one that does
        parent = os.path.dirname(target)
        if parent == target:
            raise BrowseError(f"'{path}' is not a folder")
        target = parent
    try:
        entries = list(os.scandir(target))
    except PermissionError:
        raise BrowseError(f"No permission to open '{target}'")
    except OSError as err:
        raise BrowseError(f"Cannot open '{target}': {err}")
    folders = []
    for entry in entries:
        if entry.name.startswith((".", "$")):  # hidden and system folders are of no use as a data folder
            continue
        try:
            if entry.is_dir():
                folders.append({"name": entry.name, "path": entry.path})
        except OSError:
            continue
    folders.sort(key=lambda f: f["name"].lower())
    parent = os.path.dirname(target)
    return {
        "path": target,
        "parent": "" if parent == target else parent,
        "home": home(),
        "roots": roots(),
        "folders": folders,
    }


def make_folder(parent, name, base):
    """Creates the folder `name` inside `parent` and returns its path."""
    name = (name or "").strip()
    if not name or name in (".", "..") or any(ch in INVALID_NAME_CHARS for ch in name) or name.endswith("."):
        raise BrowseError("Use a folder name without the characters  < > : \" / \\ | ? *")
    parent_path = _absolute(parent, base)
    if not os.path.isdir(parent_path):
        raise BrowseError(f"'{parent_path}' is not a folder")
    new_path = os.path.join(parent_path, name)
    try:
        os.mkdir(new_path)
    except FileExistsError:
        raise BrowseError(f"'{name}' already exists")
    except PermissionError:
        raise BrowseError(f"No permission to create a folder in '{parent_path}'")
    except OSError as err:
        raise BrowseError(f"Cannot create the folder: {err}")
    return new_path
