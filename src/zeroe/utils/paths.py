from pathlib import Path

PATH_THIS = Path(__file__).resolve()
PATH_PACKAGE = PATH_THIS.parent.parent

PATH_DATA = PATH_PACKAGE / "_data"
PATH_DATA_ATTACKS = PATH_DATA / "attacks"
PATH_DATA_MODELS  = PATH_DATA / "models"
PATH_DATA_TASKS   = PATH_DATA / "tasks"