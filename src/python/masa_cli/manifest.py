import hashlib
import json
import os

try:
    import yaml
except ImportError:
    yaml = None


class ManifestManager:
    def __init__(self, output_dir: str, format_type: str = "json"):
        self.output_dir = output_dir
        self.format_type = format_type.lower()
        self.manifest_filename = f"masa.{self.format_type}"
        self.manifest_path = os.path.join(output_dir, self.manifest_filename)
        self.records = {}
        self.load()

    def load(self) -> None:
        if not os.path.exists(self.manifest_path):
            alt_filename = "masa.yaml" if self.format_type == "json" else "masa.json"
            alt_path = os.path.join(self.output_dir, alt_filename)
            if os.path.exists(alt_path):
                self.manifest_path = alt_path
                self.format_type = "yaml" if self.format_type == "json" else "json"

        if os.path.exists(self.manifest_path):
            try:
                with open(self.manifest_path, encoding="utf-8") as f:
                    if self.format_type in ("yaml", "yml"):
                        if yaml is None:
                            raise RuntimeError("PyYAML is required to read YAML manifests")
                        data = yaml.safe_load(f)
                    else:
                        data = json.load(f)
                    if isinstance(data, dict):
                        self.records = data.get("records", {})
            except Exception:
                self.records = {}

    def save(self) -> None:
        os.makedirs(self.output_dir, exist_ok=True)
        data = {"records": self.records}
        temp_path = f"{self.manifest_path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            if self.format_type in ("yaml", "yml"):
                if yaml is None:
                    raise RuntimeError("PyYAML is required to write YAML manifests")
                yaml.safe_dump(data, f, default_flow_style=False)
            else:
                json.dump(data, f, indent=2)
        os.replace(temp_path, self.manifest_path)

    def is_processed(self, rel_input_path: str) -> bool:
        return rel_input_path in self.records

    def add_record(self, rel_input_path: str, record_data: dict) -> None:
        self.records[rel_input_path] = record_data
        self.save()


def compute_sha256(filepath: str) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()
