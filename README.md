# 3DS-Max-GLTF-Importer.
# GLTF Importer for 3ds Max

A free, open-source GLTF/GLB importer for Autodesk 3ds Max. Pure Python — no external dependencies, no compiled binaries. Just drop the script into Max and run it (evaluate it). I needed something to import a GLB and GLTF file and didn't want to pay NINETY (90) dollars U.S. for one. This was for a personal project and spending money on something that might not work (I tried a couple) was unreasonable. It worked for my purposes, but if someone else wants to tackle this beast than it's all yours. I will contribute when I can, but things are too busy for me for the near future.

![GLTF Importer_ver 0 3 0-Corona](https://github.com/user-attachments/assets/dbdb37ea-1d6b-41a2-8481-d6641604a11d)

## Features

- **GLTF 2.0 & GLB** — Supports both JSON and binary formats
- **Triangle strips & fans** — Automatic conversion with normal-verified winding
- **Multi-renderer materials** — Physical, V-Ray, Corona, and Arnold PBR material creation
- **Topology control** — Import as Triangles, Quads, or N-Gons (Editable Poly)
- **Batch import** — Import multiple files or entire folders at once
- **DPI-aware UI** — Dark-themed WinForms dialog that scales properly on high-DPI displays
- **Mesh validation** — Warns about degenerate faces, out-of-bounds indices, non-unit normals, and non-manifold edges without destroying your geometry
- **Coordinate conversion** — Y-up (GLTF default), Z-up (CAD/Max), or X-up with proper quaternion and scale handling
- **Scale presets** — Meters, Centimeters, Inches, or Custom
- **Real-time import log** — Watch progress in the UI as meshes and materials are created
- **Scene hierarchy** — Preserves node parent/child relationships with Dummy helpers

## Installation

1. Download `gltf_importer.py`
2. Place it anywhere on your system (e.g. `C:\Users\YourName\Documents\3dsMax\scripts\`)
3. In 3ds Max, open the Script Editor (or Run Script)
4. Load file
5. Evaluate all

Thank my friend Chris for the information below. Seems like a lot of work to run a script.

```python
import sys
sys.path.append(r"C:\path\to\script\folder")
import gltf_importer
gltf_importer.show_ui()
```

## Usage

### UI Mode
```python
import gltf_importer
gltf_importer.show_ui()
```

### Headless / Script Mode
```python
import gltf_importer

# Single file
gltf_importer.import_file(r"C:\models\vehicle.glb", scale=100.0)

# Batch
gltf_importer.import_batch([r"C:\models\a.gltf", r"C:\models\b.glb"])

# Folder (with optional recursion)
gltf_importer.import_folder(r"C:\models\props", scale=1.0, recursive=True)
```

## Requirements

- Autodesk 3ds Max 2022 or later (Python 3 + pymxs)
- No pip packages or external libraries needed

## Renderer Support

The importer maps GLTF PBR metallic-roughness materials to your active renderer:

| GLTF Property | Physical | V-Ray | Corona | Arnold |
|---|---|---|---|---|
| Base Color | base_color | diffuse | baseColor | base_color |
| Metallic | metalness | reflection | metalness | metalness |
| Roughness | roughness | reflection_glossiness (inv) | roughness | specular_roughness |
| Normal Map | Normal Bump | VRayNormalMap | CoronaNormal | Normal Bump |
| Emissive | emit_color | selfIllumination | selfIllumColor | emission_color |

## Version History

### v0.3.0
- DPI-aware WinForms UI
- Topology control (Triangles / Quads / N-Gons)
- Multi-renderer material support (Physical, V-Ray, Corona, Arnold)
- Triangle strip/fan conversion with normal-verified winding
- Real-time import log in UI
- Max-parented dialog window
- Non-destructive mesh validation

### v0.2.0
- Dark-themed WinForms UI with batch import
- Orientation & scale options with coordinate conversion
- Mesh integrity checks (degenerates, bounds, normals)
- ImportLog error tracking system
- Weld vertices, flip normals, auto-smooth options
- Material/texture/hierarchy toggles

### v0.1.0
- Pure Python GLTF/GLB parser
- Mesh geometry import (vertices, faces, UVs, normals)
- PBR to Physical Material conversion
- Texture extraction (embedded and external)
- Scene hierarchy with transforms
- Single-file and batch import

## Author

Richard Throgmorton — [MakIt3D.com](https://makit3d.com)

## License

MIT License — Free to use, modify, and distribute.
