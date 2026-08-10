"""Build the VS Code extension VSIX without Node/vsce.

A VSIX is an Open Packaging Conventions zip: ``[Content_Types].xml`` +
``extension.vsixmanifest`` + the extension files under ``extension/``.
``vsce`` adds validation and Marketplace publishing on top; for a
local-install extension with no build step, plain Python is enough — and it
keeps the repo's zero-Node install story true.

Usage:  python scripts/build_vsix.py
Then:   code --install-extension vscode-ext/inspeg-capture-<version>.vsix
"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

EXT_DIR = Path(__file__).resolve().parent.parent / "vscode-ext"
# Everything the extension needs at runtime; nothing else ships.
PACKAGED = ("package.json", "extension.js", "README.md", "LICENSE", "LICENSE.txt")

CONTENT_TYPES = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="json" ContentType="application/json"/>
  <Default Extension="js" ContentType="application/javascript"/>
  <Default Extension="md" ContentType="text/markdown"/>
  <Default Extension="txt" ContentType="text/plain"/>
  <Default Extension="vsixmanifest" ContentType="text/xml"/>
</Types>
"""

MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011"
                 xmlns:d="http://schemas.microsoft.com/developer/vsx-schema-design/2011">
  <Metadata>
    <Identity Language="en-US" Id="{id}" Version="{version}" Publisher="{publisher}"/>
    <DisplayName>{display_name}</DisplayName>
    <Description xml:space="preserve">{description}</Description>
    <Tags></Tags>
    <Categories>Other</Categories>
    <GalleryFlags>Public</GalleryFlags>
    <Properties>
      <Property Id="Microsoft.VisualStudio.Code.Engine" Value="{engine}"/>
      <Property Id="Microsoft.VisualStudio.Code.ExtensionDependencies" Value=""/>
      <Property Id="Microsoft.VisualStudio.Code.ExtensionPack" Value=""/>
      <Property Id="Microsoft.VisualStudio.Code.LocalizedLanguages" Value=""/>
    </Properties>
  </Metadata>
  <Installation>
    <InstallationTarget Id="Microsoft.VisualStudio.Code"/>
  </Installation>
  <Dependencies/>
  <Assets>
    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json"
           Addressable="true"/>
  </Assets>
</PackageManifest>
"""


def main() -> int:
    pkg = json.loads((EXT_DIR / "package.json").read_text(encoding="utf-8"))
    manifest = MANIFEST.format(
        id=escape(pkg["name"]),
        version=escape(pkg["version"]),
        publisher=escape(pkg["publisher"]),
        display_name=escape(pkg.get("displayName", pkg["name"])),
        description=escape(pkg.get("description", "")),
        engine=escape(pkg["engines"]["vscode"]),
    )
    out = EXT_DIR / f"{pkg['name']}-{pkg['version']}.vsix"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        zf.writestr("extension.vsixmanifest", manifest)
        packaged = []
        for name in PACKAGED:
            source = EXT_DIR / name
            if source.exists():
                zf.write(source, f"extension/{name}")
                packaged.append(name)
    print(f"built {out} ({out.stat().st_size} bytes): {', '.join(packaged)}")
    print(f'install with: code --install-extension "{out}"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
