"""Optional local-file source; set ZPP_EXAMPLE_PDF to a candidate PDF path."""
import os
from pathlib import Path

class ExampleSource:
    name = "example_source"
    def fetch(self, item_meta, dest_dir):
        value = os.environ.get("ZPP_EXAMPLE_PDF")
        return Path(value) if value and Path(value).is_file() else None

source = ExampleSource()
