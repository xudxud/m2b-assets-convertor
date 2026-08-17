# M2B

Maya 2020 to Blender static asset conversion pipeline.

## Output

Each Maya scene is exported as a portable asset package:

```text
asset_converted/
|- asset_converted.fbx
|- asset_converted.m2b.json
|- conversion_report.json
`- textures/
```

The FBX contains a static mesh snapshot and transfer Lambert materials. Diffuse and opacity textures use direct file-to-material connections so a normal Blender FBX import remains useful. The manifest preserves exact paths, channels, and semantics for the optional Blender helper.

## Material Support

- V-Ray `VRayMtl`
- V-Ray Car Paint and Fast SSS2 materials
- Maya Lambert, Phong, Blinn, and standardSurface
- Arnold aiStandardSurface

Only materials assigned to exported meshes are processed. Ambiguous multi-texture networks and unsupported materials produce magenta diagnostic materials, a report, and a failed task status instead of silently selecting an arbitrary texture.

## Desktop Workflow

- Select asset folders or drop `.mb`, `.ma`, and folders together for recursive discovery.
- Newly discovered scenes are selected by default and can be toggled individually.
- Optional opacity inversion writes separate PNG maps for white-transparent source textures.
- Source textures and Maya files are never modified.

## Static Geometry

The worker duplicates evaluated scene roots, removes construction history, deformers, joints, cameras, lights, and non-mesh shapes, then exports only the temporary static hierarchy. The source Maya file is never saved or modified.

## Development

Requirements:

- Node.js 20+
- Rust stable MSVC toolchain
- Microsoft Edge WebView2 Runtime
- Maya 2020 for integration testing

```powershell
npm install
npm run tauri dev
```

Verification:

```powershell
npm run build
cargo check --manifest-path src-tauri/Cargo.toml
```

## Worker CLI

The worker must run with Maya 2020's Python 2.7 interpreter:

```powershell
& "C:\Program Files\Autodesk\Maya2020\bin\mayapy.exe" `
  "maya_backend\maya_worker.py" `
  --input "D:\scenes\example.mb" `
  --output-dir "D:\output" `
  --copy-textures true `
  --invert-opacity false `
  --embedded-textures false `
  --overwrite rename
```

## Blender Helper

The FBX can be imported directly. For exact channel handling, UDIM paths, diagnostics, and clean Principled nodes, install `blender_addon/m2b_importer.py` in Blender 4.2 or newer and use `File > Import > M2B Asset (.m2b.json)`.

Maya, V-Ray, Arnold, Blender, and their plugins are external requirements and are not bundled.
