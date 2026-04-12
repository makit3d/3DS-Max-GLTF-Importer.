"""
GLTF Importer for 3ds Max
Version: 0.3.0
Author: Richard Throgmorton

Imports GLTF/GLB files into 3ds Max with geometry and PBR materials.
Features: Multi-file import, orientation options, mesh integrity checks,
          error detection, DPI-aware WinForms UI, Editable Poly output,
          and multi-renderer material support (Physical, V-Ray, Corona, Arnold).

Usage:
    Run from 3ds Max Python console or script editor:
        import gltf_importer
        gltf_importer.show_ui()

    Or headless:
        gltf_importer.import_file(r"C:\\path\\to\\model.gltf", scale=100.0)
"""

import json
import struct
import os
import base64
import math
import traceback

try:
    import pymxs
    rt = pymxs.runtime
    HAS_MAX = True
except ImportError:
    HAS_MAX = False
    print("Warning: pymxs not available. Running in parse-only mode.")


# ============================================================================
# IMPORT OPTIONS
# ============================================================================

class ImportOptions:
    """Configuration for GLTF import."""

    def __init__(self):
        # Orientation
        self.up_axis = 'Y'
        self.forward_axis = '-Z'
        self.scale = 1.0
        self.scale_preset = 'meters'

        # Mesh
        self.weld_vertices = False
        self.weld_threshold = 0.001
        self.auto_smooth = True
        self.smooth_angle = 45.0
        self.flip_normals = False
        self.flip_uvs_v = True
        self.remove_degenerates = True
        self.topology = 'quads'  # 'triangles', 'quads', 'ngons'

        # Materials
        self.import_materials = True
        self.import_textures = True
        self.texture_folder = ""
        self.renderer = 'physical'  # NEW: 'physical', 'vray', 'corona', 'arnold'

        # Scene
        self.import_hierarchy = True
        self.merge_by_material = False

    def get_scale_value(self):
        presets = {
            'meters': 1.0,
            'centimeters': 100.0,
            'inches': 39.3701,
            'custom': self.scale,
        }
        return presets.get(self.scale_preset, self.scale)


# ============================================================================
# IMPORT LOG / ERROR TRACKING
# ============================================================================

class ImportLog:
    """Tracks import progress, warnings, and errors."""

    def __init__(self):
        self.entries = []
        self.error_count = 0
        self.warning_count = 0
        self.mesh_count = 0
        self.material_count = 0
        self.texture_count = 0
        self.degenerate_count = 0
        self.weld_count = 0
        self._ui_textbox = None
        self._app = None

    def set_ui(self, textbox):
        """Attach a .NET TextBox for real-time log output."""
        self._ui_textbox = textbox
        try:
            import pymxs
            self._app = pymxs.runtime.dotNetClass("System.Windows.Forms.Application")
        except Exception:
            self._app = None

    def _append_ui(self, text, flush=False):
        """Append a line to the UI textbox if attached."""
        if self._ui_textbox:
            try:
                self._ui_textbox.AppendText(text + "\r\n")
                if flush and self._app:
                    self._app.DoEvents()
            except Exception:
                pass

    def info(self, msg):
        self.entries.append(('INFO', msg))
        print(f"  {msg}")
        self._append_ui(msg)

    def warn(self, msg):
        self.entries.append(('WARN', msg))
        self.warning_count += 1
        print(f"  WARNING: {msg}")
        self._append_ui(f"[WARNING] {msg}", flush=True)

    def error(self, msg):
        self.entries.append(('ERROR', msg))
        self.error_count += 1
        print(f"  ERROR: {msg}")
        self._append_ui(f"[ERROR] {msg}", flush=True)

    def section(self, msg):
        self.entries.append(('SECTION', msg))
        print(f"\n{msg}")
        self._append_ui(f"\n{'=' * 40}")
        self._append_ui(msg)
        self._append_ui('=' * 40, flush=True)

    def flush(self):
        """Force UI update."""
        if self._app:
            try:
                self._app.DoEvents()
            except Exception:
                pass

    def get_full_log(self):
        lines = []
        for level, msg in self.entries:
            if level == 'SECTION':
                lines.extend([f"\n{'=' * 50}", msg, '=' * 50])
            elif level == 'WARN':
                lines.append(f"[WARNING] {msg}")
            elif level == 'ERROR':
                lines.append(f"[ERROR] {msg}")
            else:
                lines.append(msg)
        return "\n".join(lines)


# ============================================================================
# GLTF PARSER
# ============================================================================

COMPONENT_TYPES = {
    5120: ('b', 1), 5121: ('B', 1), 5122: ('h', 2),
    5123: ('H', 2), 5125: ('I', 4), 5126: ('f', 4),
}
TYPE_COUNTS = {
    'SCALAR': 1, 'VEC2': 2, 'VEC3': 3, 'VEC4': 4,
    'MAT2': 4, 'MAT3': 9, 'MAT4': 16,
}


class GLTFData:
    def __init__(self):
        self.json_data = {}
        self.buffers = []
        self.base_dir = ""


def load_gltf(filepath):
    data = GLTFData()
    data.base_dir = os.path.dirname(os.path.abspath(filepath))
    with open(filepath, 'rb') as f:
        magic = f.read(4)
        f.seek(0)
        if magic == b'glTF':
            _parse_glb(f, data)
        else:
            content = f.read().decode('utf-8')
            data.json_data = json.loads(content)
            _load_external_buffers(data)
    return data


def _parse_glb(f, data):
    magic = struct.unpack('<I', f.read(4))[0]
    version = struct.unpack('<I', f.read(4))[0]
    length = struct.unpack('<I', f.read(4))[0]

    while f.tell() < length:
        chunk_length = struct.unpack('<I', f.read(4))[0]
        chunk_type = struct.unpack('<I', f.read(4))[0]
        chunk_data = f.read(chunk_length)

        if chunk_type == 0x4E4F534A:  # JSON
            data.json_data = json.loads(chunk_data.decode('utf-8'))
        elif chunk_type == 0x004E4942:  # BIN
            data.buffers.append(chunk_data)


def _load_external_buffers(data):
    for buf_def in data.json_data.get('buffers', []):
        uri = buf_def.get('uri', '')
        if uri.startswith('data:'):
            _, encoded = uri.split(',', 1)
            data.buffers.append(base64.b64decode(encoded))
        elif uri:
            buf_path = os.path.join(data.base_dir, uri)
            with open(buf_path, 'rb') as bf:
                data.buffers.append(bf.read())
        else:
            pass  # GLB chunk already loaded


def get_accessor_data(data, accessor_index):
    accessors = data.json_data.get('accessors', [])
    if accessor_index >= len(accessors):
        return []

    acc = accessors[accessor_index]
    bv_index = acc.get('bufferView')
    if bv_index is None:
        return []

    buffer_views = data.json_data.get('bufferViews', [])
    bv = buffer_views[bv_index]
    buf_index = bv.get('buffer', 0)

    if buf_index >= len(data.buffers):
        return []

    buf = data.buffers[buf_index]
    bv_offset = bv.get('byteOffset', 0)
    bv_stride = bv.get('byteStride', 0)
    acc_offset = acc.get('byteOffset', 0)

    comp_type = acc['componentType']
    acc_type = acc['type']
    count = acc['count']

    fmt_char, comp_size = COMPONENT_TYPES[comp_type]
    num_components = TYPE_COUNTS[acc_type]

    result = []
    offset = bv_offset + acc_offset
    stride = bv_stride if bv_stride > 0 else comp_size * num_components

    for i in range(count):
        pos = offset + i * stride
        values = []
        for c in range(num_components):
            val = struct.unpack_from(f'<{fmt_char}', buf, pos + c * comp_size)[0]
            values.append(val)
        if num_components == 1:
            result.append(values[0])
        else:
            result.append(tuple(values))

    return result


def get_image_data(data, image_index):
    images = data.json_data.get('images', [])
    if image_index >= len(images):
        return None, None

    img_def = images[image_index]
    mime = img_def.get('mimeType', 'image/png')

    if 'bufferView' in img_def:
        bv_index = img_def['bufferView']
        buffer_views = data.json_data.get('bufferViews', [])
        bv = buffer_views[bv_index]
        buf = data.buffers[bv.get('buffer', 0)]
        offset = bv.get('byteOffset', 0)
        length = bv['byteLength']
        return buf[offset:offset + length], mime

    elif 'uri' in img_def:
        uri = img_def['uri']
        if uri.startswith('data:'):
            _, encoded = uri.split(',', 1)
            return base64.b64decode(encoded), mime
        else:
            img_path = os.path.join(data.base_dir, uri)
            if os.path.exists(img_path):
                with open(img_path, 'rb') as f:
                    return f.read(), mime

    return None, None


# ============================================================================
# GLTF STRUCTURE VALIDATION
# ============================================================================

def _validate_gltf_structure(data, log):
    j = data.json_data
    asset = j.get('asset', {})
    version = asset.get('version', 'unknown')
    log.info(f"GLTF version: {version}")
    if version not in ('2.0',):
        log.warn(f"Untested GLTF version: {version}")

    if 'extensionsRequired' in j:
        for ext in j['extensionsRequired']:
            log.warn(f"Required extension not supported: {ext}")


# ============================================================================
# COORDINATE CONVERSION
# ============================================================================

def _convert_position(x, y, z, opts):
    up = opts.up_axis
    if up == 'Y':
        return (x, -z, y)
    elif up == 'Z':
        return (x, y, z)
    elif up == 'X':
        return (y, z, x)
    return (x, -z, y)


def _convert_normal(nx, ny, nz, opts):
    return _convert_position(nx, ny, nz, opts)


def _convert_quaternion(qx, qy, qz, qw, opts):
    up = opts.up_axis
    if up == 'Y':
        return (qx, -qz, qy, qw)
    elif up == 'Z':
        return (qx, qy, qz, qw)
    elif up == 'X':
        return (qy, qz, qx, qw)
    return (qx, -qz, qy, qw)


def _convert_scale_vec(sx, sy, sz, opts):
    up = opts.up_axis
    if up == 'Y':
        return (sx, sz, sy)
    elif up == 'Z':
        return (sx, sy, sz)
    elif up == 'X':
        return (sy, sz, sx)
    return (sx, sz, sy)


# ============================================================================
# MESH INTEGRITY
# ============================================================================

def _check_degenerate(positions, indices, log, mesh_name=""):
    clean = []
    removed = 0
    for i in range(0, len(indices), 3):
        if i + 2 >= len(indices):
            break
        i0, i1, i2 = indices[i], indices[i + 1], indices[i + 2]
        if i0 == i1 or i1 == i2 or i0 == i2:
            removed += 1
            continue
        if i0 < len(positions) and i1 < len(positions) and i2 < len(positions):
            p0, p1, p2 = positions[i0], positions[i1], positions[i2]
            e1 = (p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2])
            e2 = (p2[0] - p0[0], p2[1] - p0[1], p2[2] - p0[2])
            cx = e1[1] * e2[2] - e1[2] * e2[1]
            cy = e1[2] * e2[0] - e1[0] * e2[2]
            cz = e1[0] * e2[1] - e1[1] * e2[0]
            area = math.sqrt(cx * cx + cy * cy + cz * cz)
            if area < 1e-10:
                removed += 1
                continue
        clean.extend([i0, i1, i2])
    if removed > 0:
        log.degenerate_count += removed
        log.warn(f"'{mesh_name}': Removed {removed} degenerate triangle(s)")
    return clean


def _validate_indices(positions, indices, log, mesh_name=""):
    max_idx = len(positions) - 1
    clean = []
    bad = 0
    for i in range(0, len(indices), 3):
        if i + 2 >= len(indices):
            break
        i0, i1, i2 = indices[i], indices[i + 1], indices[i + 2]
        if i0 > max_idx or i1 > max_idx or i2 > max_idx:
            bad += 1
            continue
        clean.extend([i0, i1, i2])
    if bad > 0:
        log.warn(f"'{mesh_name}': Removed {bad} triangle(s) with out-of-bounds indices")
    return clean


def _validate_positions(positions, log, mesh_name=""):
    for i, p in enumerate(positions):
        if any(math.isnan(v) or math.isinf(v) for v in p):
            log.error(f"'{mesh_name}': NaN/Inf at vertex {i}")
            return False
    return True


def _validate_normals(normals, log, mesh_name=""):
    bad = 0
    for n in normals:
        length = math.sqrt(n[0]**2 + n[1]**2 + n[2]**2)
        if length < 0.9 or length > 1.1:
            bad += 1
    if bad > 0:
        log.warn(f"'{mesh_name}': {bad} normals with non-unit length")


def _validate_uvs(uvs, log, mesh_name=""):
    oob = 0
    for uv in uvs:
        if uv[0] < -0.1 or uv[0] > 1.1 or uv[1] < -0.1 or uv[1] > 1.1:
            oob += 1
    if oob > 0:
        log.info(f"'{mesh_name}': {oob} UVs outside 0-1 range (may be intentional tiling)")


def _check_flipped_faces(positions, indices, normals, log, mesh_name=""):
    flipped = 0
    checked = 0
    for i in range(0, min(len(indices), 300), 3):
        i0, i1, i2 = indices[i], indices[i+1], indices[i+2]
        if i0 >= len(positions) or i1 >= len(positions) or i2 >= len(positions):
            continue
        p0, p1, p2 = positions[i0], positions[i1], positions[i2]
        e1 = (p1[0]-p0[0], p1[1]-p0[1], p1[2]-p0[2])
        e2 = (p2[0]-p0[0], p2[1]-p0[1], p2[2]-p0[2])
        fn = (e1[1]*e2[2]-e1[2]*e2[1], e1[2]*e2[0]-e1[0]*e2[2], e1[0]*e2[1]-e1[1]*e2[0])
        if i0 < len(normals):
            vn = normals[i0]
            dot = fn[0]*vn[0] + fn[1]*vn[1] + fn[2]*vn[2]
            if dot < 0:
                flipped += 1
            checked += 1
    if checked > 0 and flipped > checked * 0.5:
        log.warn(f"'{mesh_name}': {flipped}/{checked} faces appear flipped (consider Flip Normals)")


def _check_non_manifold(indices, num_verts, log, mesh_name=""):
    edge_faces = {}
    for i in range(0, len(indices), 3):
        tri = (indices[i], indices[i+1], indices[i+2])
        for j in range(3):
            e = tuple(sorted([tri[j], tri[(j+1) % 3]]))
            edge_faces[e] = edge_faces.get(e, 0) + 1
    nm = sum(1 for c in edge_faces.values() if c > 2)
    if nm > 0:
        log.warn(f"'{mesh_name}': {nm} non-manifold edges detected")


def _check_isolated_vertices(positions, indices, log, mesh_name=""):
    used = set(indices)
    isolated = len(positions) - len(used)
    if isolated > 0 and isolated > len(positions) * 0.1:
        log.warn(f"'{mesh_name}': {isolated} isolated vertices ({isolated * 100 // len(positions)}% of total)")


def _validate_mesh_data(mesh_name, positions, normals, uvs, indices, opts, log):
    """Validate mesh data. WARN-ONLY — does not modify indices."""
    num_tris = len(indices) // 3
    log.info(f"Validating '{mesh_name}': {len(positions)} verts, {num_tris} tris")

    if not _validate_positions(positions, log, mesh_name):
        log.error(f"'{mesh_name}': Vertex data is corrupt - skipping mesh")
        return None

    # Check for out-of-bounds indices (warn only)
    max_idx = len(positions) - 1
    bad_idx = 0
    for i in range(0, len(indices), 3):
        if i + 2 >= len(indices):
            break
        i0, i1, i2 = indices[i], indices[i + 1], indices[i + 2]
        if i0 > max_idx or i1 > max_idx or i2 > max_idx:
            bad_idx += 1
    if bad_idx > 0:
        log.warn(f"'{mesh_name}': {bad_idx} triangle(s) with out-of-bounds indices")

    # Check for degenerate faces (warn only)
    if opts.remove_degenerates:
        degen = 0
        for i in range(0, len(indices), 3):
            if i + 2 >= len(indices):
                break
            i0, i1, i2 = indices[i], indices[i + 1], indices[i + 2]
            if i0 == i1 or i1 == i2 or i0 == i2:
                degen += 1
        if degen > 0:
            log.warn(f"'{mesh_name}': {degen} degenerate triangle(s) found (duplicate indices)")

    if normals:
        if len(normals) != len(positions):
            log.warn(f"'{mesh_name}': Normal count ({len(normals)}) != vertex count ({len(positions)})")
        else:
            _validate_normals(normals, log, mesh_name)
    if uvs:
        if len(uvs) != len(positions):
            log.warn(f"'{mesh_name}': UV count ({len(uvs)}) != vertex count ({len(positions)})")
        else:
            _validate_uvs(uvs, log, mesh_name)

    return indices


# ============================================================================
# 3DS MAX MESH BUILDER
# ============================================================================

def _create_max_mesh(name, positions, normals, uvs, indices, opts, log, transform=None):
    scale = opts.get_scale_value()

    if not HAS_MAX:
        log.info(f"[parse-only] Mesh '{name}': {len(positions)} verts, {len(indices) // 3} tris")
        log.mesh_count += 1
        return None

    # Full validation
    indices = _validate_mesh_data(name, positions, normals, uvs, indices, opts, log)
    if indices is None:
        return None

    num_verts = len(positions)
    num_faces = len(indices) // 3

    if num_verts == 0 or num_faces == 0:
        log.warn(f"Skipping empty mesh '{name}'")
        return None

    try:
        mesh = rt.Mesh()
        mesh.name = name
    except Exception as e:
        log.error(f"Failed to create mesh '{name}': {e}")
        return None

    rt.setNumVerts(mesh, num_verts)
    rt.setNumFaces(mesh, num_faces)

    # Vertices with coordinate conversion
    for i, pos in enumerate(positions):
        cx, cy, cz = _convert_position(pos[0], pos[1], pos[2], opts)
        rt.setVert(mesh, i + 1, rt.Point3(cx * scale, cy * scale, cz * scale))

    # Faces (1-indexed)
    for i in range(num_faces):
        v1 = indices[i * 3] + 1
        v2 = indices[i * 3 + 1] + 1
        v3 = indices[i * 3 + 2] + 1
        if opts.flip_normals:
            v2, v3 = v3, v2
        rt.setFace(mesh, i + 1, rt.Point3(v1, v2, v3))

    # UVs via meshop
    if uvs and len(uvs) == num_verts:
        try:
            rt.meshop.setNumMaps(mesh, 2)
            rt.meshop.setMapSupport(mesh, 1, True)
            rt.meshop.setNumMapVerts(mesh, 1, num_verts)
            rt.meshop.setNumMapFaces(mesh, 1, num_faces)
            for i, uv in enumerate(uvs):
                u = uv[0]
                v = (1.0 - uv[1]) if opts.flip_uvs_v else uv[1]
                rt.meshop.setMapVert(mesh, 1, i + 1, rt.Point3(u, v, 0.0))
            for i in range(num_faces):
                v1 = indices[i * 3] + 1
                v2 = indices[i * 3 + 1] + 1
                v3 = indices[i * 3 + 2] + 1
                if opts.flip_normals:
                    v2, v3 = v3, v2
                rt.meshop.setMapFace(mesh, 1, i + 1, rt.Point3(v1, v2, v3))
        except Exception as e:
            log.warn(f"Failed to set UVs on '{name}': {e}")

    # Per-vertex normals from GLTF data
    if normals and len(normals) == num_verts:
        for i, nrm in enumerate(normals):
            nx, ny, nz = _convert_normal(nrm[0], nrm[1], nrm[2], opts)
            rt.setNormal(mesh, i + 1, rt.Point3(nx, ny, nz))

    # Node transform
    if transform:
        _apply_node_transform(mesh, transform, opts)

    rt.update(mesh)

    # Diagnostic: verify face count after update
    try:
        actual_faces = rt.getNumFaces(mesh)
        actual_verts = rt.getNumVerts(mesh)
        if actual_faces != num_faces:
            log.warn(f"'{name}': Expected {num_faces} faces, Max reports {actual_faces}")
        log.info(f"'{name}': Created {actual_verts} verts, {actual_faces} faces")
    except Exception:
        pass

    # Disable backface culling
    try:
        mesh.backfacecull = False
    except Exception:
        try:
            rt.setProperty(mesh, "backfacecull", False)
        except Exception:
            pass

    # Weld vertices
    if opts.weld_vertices:
        try:
            before = rt.getNumVerts(mesh)
            rt.meshop.weldVertsByThreshold(mesh, rt.getNumVerts(mesh), opts.weld_threshold)
            after = rt.getNumVerts(mesh)
            welded = before - after
            if welded > 0:
                log.weld_count += welded
                log.info(f"Welded {welded} vertices on '{name}'")
        except Exception as e:
            log.warn(f"Weld failed on '{name}': {e}")

    # Auto-smooth (only when no explicit normals — avoids conflicts)
    if opts.auto_smooth and not normals:
        try:
            rt.addModifier(mesh, rt.Smooth(autoSmooth=True, threshold=opts.smooth_angle))
        except Exception:
            pass

    # Convert to Editable Poly with topology control
    if opts.topology != 'mesh':
        try:
            if opts.topology == 'triangles':
                # Turn_to_Poly with max 3 sides keeps triangles
                ttp = rt.Turn_to_Poly()
                ttp.limitPolySize = True
                ttp.maxPolySize = 3
                rt.addModifier(mesh, ttp)
                rt.convertToPoly(mesh)
                log.info(f"'{name}': Editable Poly (triangles)")
            elif opts.topology == 'quads':
                # Turn_to_Poly with max 4 sides merges tri pairs into quads
                ttp = rt.Turn_to_Poly()
                ttp.limitPolySize = True
                ttp.maxPolySize = 4
                rt.addModifier(mesh, ttp)
                rt.convertToPoly(mesh)
                log.info(f"'{name}': Editable Poly (quads)")
            else:
                # N-gons: straight conversion, merges coplanar faces freely
                rt.convertToPoly(mesh)
                log.info(f"'{name}': Editable Poly (n-gons)")
        except Exception as e:
            log.warn(f"Failed topology conversion on '{name}': {e}")
            # Fallback: try simple convertToPoly
            try:
                rt.convertToPoly(mesh)
            except Exception:
                pass

    log.mesh_count += 1
    log.flush()
    return mesh


def _apply_node_transform(node, transform, opts):
    if not HAS_MAX:
        return
    scale = opts.get_scale_value()
    if 'translation' in transform:
        t = transform['translation']
        tx, ty, tz = _convert_position(t[0], t[1], t[2], opts)
        node.pos = rt.Point3(tx * scale, ty * scale, tz * scale)
    if 'rotation' in transform:
        q = transform['rotation']
        qx, qy, qz, qw = _convert_quaternion(q[0], q[1], q[2], q[3], opts)
        node.rotation = rt.Quat(qx, qy, qz, qw)
    if 'scale' in transform:
        s = transform['scale']
        sx, sy, sz = _convert_scale_vec(s[0], s[1], s[2], opts)
        node.scale = rt.Point3(sx, sy, sz)


# ============================================================================
# TEXTURE HANDLING
# ============================================================================

def _save_texture(data, tex_index, output_dir, log):
    textures = data.json_data.get('textures', [])
    if tex_index >= len(textures):
        return None
    source = textures[tex_index].get('source', 0)
    images = data.json_data.get('images', [])
    if source >= len(images):
        return None
    img_def = images[source]
    name = img_def.get('name', f'texture_{tex_index}')
    ext = '.png' if 'png' in img_def.get('mimeType', 'image/png') else '.jpg'
    if not name.lower().endswith(('.png', '.jpg', '.jpeg')):
        name += ext
    fp = os.path.join(output_dir, name)
    if os.path.exists(fp):
        return fp
    img_data, _ = get_image_data(data, source)
    if img_data:
        os.makedirs(output_dir, exist_ok=True)
        try:
            with open(fp, 'wb') as f:
                f.write(img_data)
            log.texture_count += 1
            return fp
        except Exception as e:
            log.error(f"Failed to save texture '{name}': {e}")
    return None


# ============================================================================
# MATERIAL BUILDERS (Multi-Renderer)
# ============================================================================

def _create_bitmap(tex_path):
    """Create a BitmapTexture from a file path."""
    bm = rt.BitmapTexture()
    bm.filename = tex_path
    return bm


def _create_physical_material(name, mat_def, data, texture_dir, opts, log):
    """Create a 3ds Max Physical Material."""
    pbr = mat_def.get('pbrMetallicRoughness', {})
    try:
        mat = rt.PhysicalMaterial()
        mat.name = name
    except Exception as e:
        log.error(f"Failed to create Physical material '{name}': {e}")
        return None

    bc = pbr.get('baseColorFactor', [1, 1, 1, 1])
    mat.base_color = rt.Color(bc[0] * 255, bc[1] * 255, bc[2] * 255)

    if opts.import_textures and 'baseColorTexture' in pbr:
        tex_path = _save_texture(data, pbr['baseColorTexture']['index'], texture_dir, log)
        if tex_path:
            try:
                mat.base_color_map = _create_bitmap(tex_path)
            except Exception as e:
                log.warn(f"Failed to assign base color texture: {e}")

    mat.metalness = pbr.get('metallicFactor', 1.0)
    mat.roughness = pbr.get('roughnessFactor', 1.0)

    if opts.import_textures and 'metallicRoughnessTexture' in pbr:
        tex_path = _save_texture(data, pbr['metallicRoughnessTexture']['index'], texture_dir, log)
        if tex_path:
            try:
                mat.metalness_map = _create_bitmap(tex_path)
                mat.roughness_map = _create_bitmap(tex_path)
            except Exception as e:
                log.warn(f"Failed to assign metallic/roughness texture: {e}")

    if opts.import_textures and 'normalTexture' in mat_def:
        tex_path = _save_texture(data, mat_def['normalTexture']['index'], texture_dir, log)
        if tex_path:
            try:
                normal_map = rt.Normal_Bump()
                normal_map.normal_map = _create_bitmap(tex_path)
                normal_map.mult_spin = mat_def['normalTexture'].get('scale', 1.0)
                mat.bump_map = normal_map
            except Exception as e:
                log.warn(f"Failed to assign normal map: {e}")

    emissive = mat_def.get('emissiveFactor', [0, 0, 0])
    if any(e > 0 for e in emissive):
        mat.emission = 1.0
        mat.emit_color = rt.Color(emissive[0] * 255, emissive[1] * 255, emissive[2] * 255)

    if opts.import_textures and 'emissiveTexture' in mat_def:
        tex_path = _save_texture(data, mat_def['emissiveTexture']['index'], texture_dir, log)
        if tex_path:
            try:
                mat.emit_color_map = _create_bitmap(tex_path)
            except Exception as e:
                log.warn(f"Failed to assign emissive texture: {e}")

    alpha_mode = mat_def.get('alphaMode', 'OPAQUE')
    if alpha_mode == 'BLEND':
        mat.transparency = 1.0 - bc[3]
    elif alpha_mode == 'MASK':
        mat.transparency = 1.0 - mat_def.get('alphaCutoff', 0.5)

    if mat_def.get('doubleSided', False):
        mat.thin_walled = True

    return mat


def _create_vray_material(name, mat_def, data, texture_dir, opts, log):
    """Create a V-Ray material (VRayMtl)."""
    pbr = mat_def.get('pbrMetallicRoughness', {})

    # Try VRayMtl first, then VRayBRDF
    mat = None
    for cls_name in ('VRayMtl', 'VRayBRDF'):
        try:
            mat_class = getattr(rt, cls_name, None)
            if mat_class:
                mat = mat_class()
                mat.name = name
                break
        except Exception:
            continue

    if mat is None:
        log.error(f"V-Ray not available. Cannot create material '{name}'")
        return None

    bc = pbr.get('baseColorFactor', [1, 1, 1, 1])

    # Diffuse color
    try:
        mat.diffuse = rt.Color(bc[0] * 255, bc[1] * 255, bc[2] * 255)
    except Exception:
        try:
            mat.Diffuse = rt.Color(bc[0] * 255, bc[1] * 255, bc[2] * 255)
        except Exception as e:
            log.warn(f"Could not set V-Ray diffuse color: {e}")

    # Base color texture
    if opts.import_textures and 'baseColorTexture' in pbr:
        tex_path = _save_texture(data, pbr['baseColorTexture']['index'], texture_dir, log)
        if tex_path:
            try:
                mat.texmap_diffuse = _create_bitmap(tex_path)
                mat.texmap_diffuse_on = True
            except Exception as e:
                log.warn(f"V-Ray: Failed to assign diffuse texture: {e}")

    # Metallic → reflection color (white for metallic, use IOR for non-metallic)
    metallic = pbr.get('metallicFactor', 1.0)
    try:
        refl_val = int(metallic * 255)
        mat.reflection = rt.Color(refl_val, refl_val, refl_val)
        mat.reflection_fresnel = True
        if metallic > 0.5:
            mat.reflection_lockIOR = False
            mat.reflection_ior = 50.0  # High IOR for metallic look
        else:
            mat.reflection_lockIOR = True
    except Exception as e:
        log.warn(f"V-Ray: Failed to set reflection: {e}")

    # Roughness → reflection glossiness (inverted)
    roughness = pbr.get('roughnessFactor', 1.0)
    try:
        mat.reflection_glossiness = 1.0 - roughness
    except Exception as e:
        log.warn(f"V-Ray: Failed to set glossiness: {e}")

    # Metallic-Roughness texture
    if opts.import_textures and 'metallicRoughnessTexture' in pbr:
        tex_path = _save_texture(data, pbr['metallicRoughnessTexture']['index'], texture_dir, log)
        if tex_path:
            try:
                mat.texmap_reflection = _create_bitmap(tex_path)
                mat.texmap_reflection_on = True
                mat.texmap_reflectionGlossiness = _create_bitmap(tex_path)
                mat.texmap_reflectionGlossiness_on = True
            except Exception as e:
                log.warn(f"V-Ray: Failed to assign metallic/roughness texture: {e}")

    # Normal map
    if opts.import_textures and 'normalTexture' in mat_def:
        tex_path = _save_texture(data, mat_def['normalTexture']['index'], texture_dir, log)
        if tex_path:
            try:
                vray_normal = rt.VRayNormalMap()
                vray_normal.normal_map = _create_bitmap(tex_path)
                vray_normal.mult_spin = mat_def['normalTexture'].get('scale', 1.0)
                mat.texmap_bump = vray_normal
                mat.texmap_bump_on = True
                mat.texmap_bump_multiplier = mat_def['normalTexture'].get('scale', 1.0)
            except Exception:
                # Fallback to standard Normal Bump
                try:
                    normal_map = rt.Normal_Bump()
                    normal_map.normal_map = _create_bitmap(tex_path)
                    mat.texmap_bump = normal_map
                    mat.texmap_bump_on = True
                except Exception as e:
                    log.warn(f"V-Ray: Failed to assign normal map: {e}")

    # Emissive
    emissive = mat_def.get('emissiveFactor', [0, 0, 0])
    if any(e > 0 for e in emissive):
        try:
            mat.selfIllumination = rt.Color(emissive[0] * 255, emissive[1] * 255, emissive[2] * 255)
            mat.selfIllumination_gi = True
        except Exception:
            pass

    if opts.import_textures and 'emissiveTexture' in mat_def:
        tex_path = _save_texture(data, mat_def['emissiveTexture']['index'], texture_dir, log)
        if tex_path:
            try:
                mat.texmap_self_illumination = _create_bitmap(tex_path)
                mat.texmap_self_illumination_on = True
            except Exception as e:
                log.warn(f"V-Ray: Failed to assign emissive texture: {e}")

    # Alpha
    alpha_mode = mat_def.get('alphaMode', 'OPAQUE')
    if alpha_mode == 'BLEND':
        try:
            opacity_val = int(bc[3] * 255)
            mat.refraction = rt.Color(255 - opacity_val, 255 - opacity_val, 255 - opacity_val)
        except Exception:
            pass

    # Double-sided
    if mat_def.get('doubleSided', False):
        try:
            mat.option_doubleSided = True
        except Exception:
            pass

    return mat


def _create_corona_material(name, mat_def, data, texture_dir, opts, log):
    """Create a Corona material (CoronaPhysicalMtl or CoronaMtl)."""
    pbr = mat_def.get('pbrMetallicRoughness', {})

    mat = None
    for cls_name in ('CoronaPhysicalMtl', 'CoronaMtl'):
        try:
            mat_class = getattr(rt, cls_name, None)
            if mat_class:
                mat = mat_class()
                mat.name = name
                break
        except Exception:
            continue

    if mat is None:
        log.error(f"Corona not available. Cannot create material '{name}'")
        return None

    bc = pbr.get('baseColorFactor', [1, 1, 1, 1])
    is_physical = hasattr(mat, 'baseColor')

    # Base color
    try:
        if is_physical:
            mat.baseColor = rt.Color(bc[0] * 255, bc[1] * 255, bc[2] * 255)
        else:
            mat.colorDiffuse = rt.Color(bc[0] * 255, bc[1] * 255, bc[2] * 255)
    except Exception as e:
        log.warn(f"Corona: Failed to set base color: {e}")

    # Base color texture
    if opts.import_textures and 'baseColorTexture' in pbr:
        tex_path = _save_texture(data, pbr['baseColorTexture']['index'], texture_dir, log)
        if tex_path:
            try:
                if is_physical:
                    mat.baseTexmap = _create_bitmap(tex_path)
                else:
                    mat.texmapDiffuse = _create_bitmap(tex_path)
            except Exception as e:
                log.warn(f"Corona: Failed to assign base color texture: {e}")

    # Metallic
    metallic = pbr.get('metallicFactor', 1.0)
    try:
        if is_physical:
            mat.metalnessMode = 1  # Enable metalness workflow
            mat.metalness = metallic
        else:
            mat.levelDiffuse = 1.0 - metallic
            refl_val = metallic
            mat.levelReflect = refl_val
    except Exception as e:
        log.warn(f"Corona: Failed to set metalness: {e}")

    # Roughness
    roughness = pbr.get('roughnessFactor', 1.0)
    try:
        if is_physical:
            mat.roughness = roughness
        else:
            mat.glossiness = 1.0 - roughness
    except Exception as e:
        log.warn(f"Corona: Failed to set roughness: {e}")

    # Metallic-Roughness texture
    if opts.import_textures and 'metallicRoughnessTexture' in pbr:
        tex_path = _save_texture(data, pbr['metallicRoughnessTexture']['index'], texture_dir, log)
        if tex_path:
            try:
                if is_physical:
                    mat.metalnessTexmap = _create_bitmap(tex_path)
                    mat.roughnessTexmap = _create_bitmap(tex_path)
                else:
                    mat.texmapReflect = _create_bitmap(tex_path)
                    mat.texmapReflectGlossiness = _create_bitmap(tex_path)
            except Exception as e:
                log.warn(f"Corona: Failed to assign metallic/roughness texture: {e}")

    # Normal map
    if opts.import_textures and 'normalTexture' in mat_def:
        tex_path = _save_texture(data, mat_def['normalTexture']['index'], texture_dir, log)
        if tex_path:
            try:
                corona_normal = getattr(rt, 'CoronaNormal', None)
                if corona_normal:
                    cn = corona_normal()
                    cn.normalMap = _create_bitmap(tex_path)
                    cn.multiplier = mat_def['normalTexture'].get('scale', 1.0)
                    if is_physical:
                        mat.baseBumpTexmap = cn
                    else:
                        mat.texmapBump = cn
                else:
                    normal_map = rt.Normal_Bump()
                    normal_map.normal_map = _create_bitmap(tex_path)
                    if is_physical:
                        mat.baseBumpTexmap = normal_map
                    else:
                        mat.texmapBump = normal_map
            except Exception as e:
                log.warn(f"Corona: Failed to assign normal map: {e}")

    # Emissive
    emissive = mat_def.get('emissiveFactor', [0, 0, 0])
    if any(e > 0 for e in emissive):
        try:
            if is_physical:
                mat.selfIllumColor = rt.Color(emissive[0] * 255, emissive[1] * 255, emissive[2] * 255)
                mat.selfIllumLevel = 1.0
            else:
                mat.selfIllumColor = rt.Color(emissive[0] * 255, emissive[1] * 255, emissive[2] * 255)
        except Exception:
            pass

    if opts.import_textures and 'emissiveTexture' in mat_def:
        tex_path = _save_texture(data, mat_def['emissiveTexture']['index'], texture_dir, log)
        if tex_path:
            try:
                if is_physical:
                    mat.selfIllumTexmap = _create_bitmap(tex_path)
                else:
                    mat.texmapSelfIllum = _create_bitmap(tex_path)
            except Exception as e:
                log.warn(f"Corona: Failed to assign emissive texture: {e}")

    # Alpha
    alpha_mode = mat_def.get('alphaMode', 'OPAQUE')
    if alpha_mode == 'BLEND':
        try:
            if is_physical:
                mat.opacity = bc[3]
            else:
                mat.levelOpacity = bc[3]
        except Exception:
            pass

    return mat


def _create_arnold_material(name, mat_def, data, texture_dir, opts, log):
    """Create an Arnold material (ai_standard_surface)."""
    pbr = mat_def.get('pbrMetallicRoughness', {})

    mat = None
    for cls_name in ('ai_standard_surface', 'aiStandardSurface', 'AiStandardSurface'):
        try:
            mat_class = getattr(rt, cls_name, None)
            if mat_class:
                mat = mat_class()
                mat.name = name
                break
        except Exception:
            continue

    if mat is None:
        log.error(f"Arnold not available. Cannot create material '{name}'")
        return None

    bc = pbr.get('baseColorFactor', [1, 1, 1, 1])

    # Base color
    try:
        mat.base_color = rt.Color(bc[0] * 255, bc[1] * 255, bc[2] * 255)
    except Exception:
        try:
            mat.baseColor = rt.Color(bc[0] * 255, bc[1] * 255, bc[2] * 255)
        except Exception as e:
            log.warn(f"Arnold: Failed to set base color: {e}")

    # Base weight
    try:
        mat.base = 1.0
    except Exception:
        pass

    # Base color texture
    if opts.import_textures and 'baseColorTexture' in pbr:
        tex_path = _save_texture(data, pbr['baseColorTexture']['index'], texture_dir, log)
        if tex_path:
            try:
                mat.base_color_map = _create_bitmap(tex_path)
            except Exception:
                try:
                    mat.baseColor_map = _create_bitmap(tex_path)
                except Exception as e:
                    log.warn(f"Arnold: Failed to assign base color texture: {e}")

    # Metallic
    metallic = pbr.get('metallicFactor', 1.0)
    try:
        mat.metalness = metallic
    except Exception as e:
        log.warn(f"Arnold: Failed to set metalness: {e}")

    # Specular roughness
    roughness = pbr.get('roughnessFactor', 1.0)
    try:
        mat.specular_roughness = roughness
    except Exception:
        try:
            mat.specularRoughness = roughness
        except Exception as e:
            log.warn(f"Arnold: Failed to set roughness: {e}")

    # Specular weight
    try:
        mat.specular = 1.0
    except Exception:
        pass

    # Metallic-Roughness texture
    if opts.import_textures and 'metallicRoughnessTexture' in pbr:
        tex_path = _save_texture(data, pbr['metallicRoughnessTexture']['index'], texture_dir, log)
        if tex_path:
            try:
                mat.metalness_map = _create_bitmap(tex_path)
                mat.specular_roughness_map = _create_bitmap(tex_path)
            except Exception as e:
                log.warn(f"Arnold: Failed to assign metallic/roughness texture: {e}")

    # Normal map
    if opts.import_textures and 'normalTexture' in mat_def:
        tex_path = _save_texture(data, mat_def['normalTexture']['index'], texture_dir, log)
        if tex_path:
            try:
                normal_map = rt.Normal_Bump()
                normal_map.normal_map = _create_bitmap(tex_path)
                normal_map.mult_spin = mat_def['normalTexture'].get('scale', 1.0)
                mat.bump_map = normal_map
            except Exception:
                try:
                    mat.normal_map = _create_bitmap(tex_path)
                except Exception as e:
                    log.warn(f"Arnold: Failed to assign normal map: {e}")

    # Emissive
    emissive = mat_def.get('emissiveFactor', [0, 0, 0])
    if any(e > 0 for e in emissive):
        try:
            mat.emission = 1.0
            mat.emission_color = rt.Color(emissive[0] * 255, emissive[1] * 255, emissive[2] * 255)
        except Exception:
            try:
                mat.emissionColor = rt.Color(emissive[0] * 255, emissive[1] * 255, emissive[2] * 255)
            except Exception:
                pass

    if opts.import_textures and 'emissiveTexture' in mat_def:
        tex_path = _save_texture(data, mat_def['emissiveTexture']['index'], texture_dir, log)
        if tex_path:
            try:
                mat.emission_color_map = _create_bitmap(tex_path)
            except Exception:
                try:
                    mat.emissionColor_map = _create_bitmap(tex_path)
                except Exception as e:
                    log.warn(f"Arnold: Failed to assign emissive texture: {e}")

    # Alpha
    alpha_mode = mat_def.get('alphaMode', 'OPAQUE')
    if alpha_mode == 'BLEND':
        try:
            mat.opacity = rt.Color(bc[3] * 255, bc[3] * 255, bc[3] * 255)
        except Exception:
            pass

    # Thin-walled (double-sided)
    if mat_def.get('doubleSided', False):
        try:
            mat.thin_walled = True
        except Exception:
            pass

    return mat


def _create_max_material(name, mat_def, data, texture_dir, opts, log):
    """Create a material using the selected renderer."""
    if not HAS_MAX:
        log.material_count += 1
        return None

    renderer = opts.renderer.lower()

    if renderer == 'vray':
        mat = _create_vray_material(name, mat_def, data, texture_dir, opts, log)
    elif renderer == 'corona':
        mat = _create_corona_material(name, mat_def, data, texture_dir, opts, log)
    elif renderer == 'arnold':
        mat = _create_arnold_material(name, mat_def, data, texture_dir, opts, log)
    else:
        mat = _create_physical_material(name, mat_def, data, texture_dir, opts, log)

    if mat is not None:
        log.material_count += 1
    return mat


# ============================================================================
# SCENE BUILDER
# ============================================================================

def _process_mesh(data, mesh_index, materials, texture_dir, opts, log):
    meshes = data.json_data.get('meshes', [])
    if mesh_index >= len(meshes):
        return []
    md = meshes[mesh_index]
    mn = md.get('name', f'Mesh_{mesh_index}')
    results = []

    for pi, prim in enumerate(md.get('primitives', [])):
        attr = prim.get('attributes', {})
        mode = prim.get('mode', 4)  # Default: TRIANGLES
        pos, nrm, uv, idx = None, None, None, None
        try:
            if 'POSITION' in attr:
                pos = get_accessor_data(data, attr['POSITION'])
            if 'NORMAL' in attr:
                nrm = get_accessor_data(data, attr['NORMAL'])
            if 'TEXCOORD_0' in attr:
                uv = get_accessor_data(data, attr['TEXCOORD_0'])
            if 'indices' in prim:
                idx = get_accessor_data(data, prim['indices'])
            elif pos:
                idx = list(range(len(pos)))
        except Exception as e:
            log.error(f"Failed to read geometry for '{mn}': {e}")
            continue

        if not pos or not idx:
            continue

        # Convert triangle strips/fans to triangle lists
        if mode == 5:  # TRIANGLE_STRIP
            log.info(f"'{mn}': Converting triangle strip ({len(idx)} indices)")
            tri_indices = []
            for i in range(len(idx) - 2):
                a, b, c = idx[i], idx[i + 1], idx[i + 2]
                # Skip degenerate triangles (strip restart markers)
                if a == b or b == c or a == c:
                    continue
                if i % 2 == 0:
                    tri_indices.extend([a, b, c])
                else:
                    tri_indices.extend([a, c, b])

            # Use vertex normals to verify and fix winding on each triangle
            if nrm and len(nrm) == len(pos):
                fixed = 0
                for t in range(0, len(tri_indices), 3):
                    i0, i1, i2 = tri_indices[t], tri_indices[t + 1], tri_indices[t + 2]
                    if i0 >= len(pos) or i1 >= len(pos) or i2 >= len(pos):
                        continue
                    p0, p1, p2 = pos[i0], pos[i1], pos[i2]
                    # Compute face normal via cross product
                    e1 = (p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2])
                    e2 = (p2[0] - p0[0], p2[1] - p0[1], p2[2] - p0[2])
                    fn_x = e1[1] * e2[2] - e1[2] * e2[1]
                    fn_y = e1[2] * e2[0] - e1[0] * e2[2]
                    fn_z = e1[0] * e2[1] - e1[1] * e2[0]
                    # Dot with vertex normal — negative means wrong winding
                    vn = nrm[i0]
                    dot = fn_x * vn[0] + fn_y * vn[1] + fn_z * vn[2]
                    if dot < 0:
                        tri_indices[t + 1], tri_indices[t + 2] = tri_indices[t + 2], tri_indices[t + 1]
                        fixed += 1
                if fixed > 0:
                    log.info(f"'{mn}': Fixed winding on {fixed} strip triangles")

            log.info(f"'{mn}': Strip produced {len(tri_indices) // 3} triangles")
            idx = tri_indices
        elif mode == 6:  # TRIANGLE_FAN
            log.info(f"'{mn}': Converting triangle fan ({len(idx)} indices)")
            tri_indices = []
            for i in range(1, len(idx) - 1):
                tri_indices.extend([idx[0], idx[i], idx[i + 1]])
            idx = tri_indices
        elif mode != 4:
            log.warn(f"'{mn}': Unsupported primitive mode {mode}, skipping")
            continue

        pn = mn if len(md['primitives']) == 1 else f"{mn}_{pi}"
        log.info(f"'{pn}': {len(pos)} verts, {len(idx) // 3} tris (mode {mode})")
        mm = _create_max_mesh(pn, pos, nrm, uv, idx, opts, log)
        mi = prim.get('material', -1)
        mat = materials.get(mi) if mi >= 0 else None
        if mm and mat:
            mm.material = mat
        results.append((mm, mi))

    return results


def _process_node(data, ni, materials, td, opts, log, parent=None, depth=0):
    nodes = data.json_data.get('nodes', [])
    if ni >= len(nodes):
        return []
    nd = nodes[ni]
    nn = nd.get('name', f'Node_{ni}')
    created = []

    if 'mesh' in nd:
        for mm, mi in _process_mesh(data, nd['mesh'], materials, td, opts, log):
            if mm:
                tr = {k: nd[k] for k in ('translation', 'rotation', 'scale') if k in nd}
                if tr:
                    _apply_node_transform(mm, tr, opts)
                if parent and HAS_MAX:
                    mm.parent = parent
                created.append(mm)
                log.info(f"{'  ' * depth}Mesh: {mm.name if HAS_MAX else nn}")
    elif opts.import_hierarchy and 'children' in nd and HAS_MAX:
        try:
            h = rt.Dummy()
            h.name = nn
            tr = {k: nd[k] for k in ('translation', 'rotation', 'scale') if k in nd}
            if tr:
                _apply_node_transform(h, tr, opts)
            if parent:
                h.parent = parent
            created.append(h)
        except Exception:
            pass

    pn = created[0] if created else parent
    for ci in nd.get('children', []):
        created.extend(_process_node(data, ci, materials, td, opts, log, parent=pn, depth=depth + 1))
    return created


# ============================================================================
# PUBLIC API
# ============================================================================

def import_file(filepath, opts=None, log=None, scale=1.0, scene_index=0):
    if opts is None:
        opts = ImportOptions()
        opts.scale = scale
        opts.scale_preset = 'custom'
    if log is None:
        log = ImportLog()

    filepath = os.path.abspath(filepath)
    if not os.path.exists(filepath):
        log.error(f"File not found: {filepath}")
        return [], log

    log.section(f"Importing: {os.path.basename(filepath)}")
    log.info(f"Scale: {opts.get_scale_value()} | Up: {opts.up_axis} | Renderer: {opts.renderer}")
    log.info(f"Topology: {opts.topology}")

    try:
        data = load_gltf(filepath)
    except Exception as e:
        log.error(f"Failed to parse GLTF: {e}")
        return [], log

    _validate_gltf_structure(data, log)

    j = data.json_data
    meshes = j.get('meshes', [])
    mat_defs = j.get('materials', [])
    nodes = j.get('nodes', [])
    scenes = j.get('scenes', [])

    log.info(f"Meshes: {len(meshes)} | Materials: {len(mat_defs)} | Nodes: {len(nodes)}")

    # Texture output directory
    texture_dir = opts.texture_folder if opts.texture_folder else os.path.join(data.base_dir, 'textures')

    # Create materials
    materials = {}
    if opts.import_materials:
        log.section("Creating materials")
        for i, md in enumerate(mat_defs):
            mat_name = md.get('name', f'Material_{i}')
            mat = _create_max_material(mat_name, md, data, texture_dir, opts, log)
            if mat:
                materials[i] = mat
                log.info(f"  [{i}] {mat_name} ({opts.renderer})")

    # Process scene
    log.section("Building scene")
    created = []
    if scenes:
        si = min(scene_index, len(scenes) - 1)
        scene = scenes[si]
        for ni in scene.get('nodes', []):
            created.extend(_process_node(data, ni, materials, texture_dir, opts, log))
    else:
        for ni in range(len(nodes)):
            created.extend(_process_node(data, ni, materials, texture_dir, opts, log))

    log.section("Summary")
    log.info(f"Meshes: {log.mesh_count} | Materials: {log.material_count} | Textures: {log.texture_count}")
    if log.degenerate_count > 0:
        log.info(f"Degenerate triangles removed: {log.degenerate_count}")
    if log.weld_count > 0:
        log.info(f"Vertices welded: {log.weld_count}")
    if log.warning_count > 0:
        log.info(f"Warnings: {log.warning_count}")
    if log.error_count > 0:
        log.info(f"Errors: {log.error_count}")

    return created, log


def import_batch(filepaths, opts=None, log=None):
    if opts is None:
        opts = ImportOptions()
    if log is None:
        log = ImportLog()
    all_created = []
    for i, fp in enumerate(filepaths):
        log.section(f"[{i + 1}/{len(filepaths)}] {os.path.basename(fp)}")
        nodes, _ = import_file(fp, opts=opts, log=log)
        all_created.extend(nodes)
    return all_created, log


def import_folder(folder_path, scale=1.0, recursive=False):
    opts = ImportOptions()
    opts.scale = scale
    opts.scale_preset = 'custom'
    folder_path = os.path.abspath(folder_path)
    if not os.path.isdir(folder_path):
        print(f"Error: Folder not found: {folder_path}")
        return []
    files = []
    if recursive:
        for root, dirs, filenames in os.walk(folder_path):
            for fn in filenames:
                if fn.lower().endswith(('.gltf', '.glb')):
                    files.append(os.path.join(root, fn))
    else:
        for fn in os.listdir(folder_path):
            if fn.lower().endswith(('.gltf', '.glb')):
                files.append(os.path.join(folder_path, fn))
    files.sort()
    if not files:
        print(f"No GLTF/GLB files found in: {folder_path}")
        return []
    all_created, log = import_batch(files, opts=opts)
    return all_created


# ============================================================================
# DPI-AWARE WINFORMS UI
# ============================================================================

def show_ui():
    if not HAS_MAX:
        print("Error: Must be run from 3ds Max")
        return
    _build_ui()


def _dpi_scale():
    """Get the current DPI scale factor."""
    try:
        graphics = rt.dotNetClass("System.Drawing.Graphics")
        screen = rt.dotNetClass("System.Windows.Forms.Screen")
        # Use CreateGraphics on a temp form to get DPI
        temp = rt.dotNetObject("System.Windows.Forms.Form")
        g = temp.CreateGraphics()
        dpi_x = g.DpiX
        g.Dispose()
        temp.Dispose()
        return dpi_x / 96.0
    except Exception:
        return 1.0


def _build_ui():
    """Build and show the .NET WinForms import dialog with DPI awareness."""

    # Enable DPI awareness
    try:
        app = rt.dotNetClass("System.Windows.Forms.Application")
        app.EnableVisualStyles()
    except Exception:
        pass

    try:
        set_dpi = rt.dotNetClass("System.Windows.Forms.Application")
        set_dpi.SetHighDpiMode(set_dpi.SetHighDpiMode.SystemAware)
    except Exception:
        pass

    # Get DPI scale factor
    dpi = _dpi_scale()

    def S(val):
        """Scale a pixel value by DPI."""
        return int(val * dpi)

    Color = rt.dotNetClass("System.Drawing.Color")
    FormStartPosition = rt.dotNetClass("System.Windows.Forms.FormStartPosition")
    FormBorderStyle = rt.dotNetClass("System.Windows.Forms.FormBorderStyle")
    ComboBoxStyle = rt.dotNetClass("System.Windows.Forms.ComboBoxStyle")
    SelectionMode = rt.dotNetClass("System.Windows.Forms.SelectionMode")
    ScrollBars = rt.dotNetClass("System.Windows.Forms.ScrollBars")
    FlatStyle = rt.dotNetClass("System.Windows.Forms.FlatStyle")
    BorderStyle_enum = rt.dotNetClass("System.Windows.Forms.BorderStyle")
    FontStyle = rt.dotNetClass("System.Drawing.FontStyle")

    form = rt.dotNetObject("System.Windows.Forms.Form")
    form.Text = "GLTF Importer v0.3.0"
    form.Size = rt.dotNetObject("System.Drawing.Size", S(600), S(770))
    form.StartPosition = FormStartPosition.CenterScreen
    form.FormBorderStyle = FormBorderStyle.FixedDialog
    form.MaximizeBox = False
    form.BackColor = Color.FromArgb(45, 45, 48)
    form.ForeColor = Color.FromArgb(220, 220, 220)

    small_font = rt.dotNetObject("System.Drawing.Font", "Segoe UI", 8.5)
    bold_font = rt.dotNetObject("System.Drawing.Font", "Segoe UI", 9.0, FontStyle.Bold)

    # File paths stored in Python (not from ListBox)
    _file_paths = []

    y = S(10)

    # ---- FILE LIST ----
    files_group = rt.dotNetObject("System.Windows.Forms.GroupBox")
    files_group.Text = "Files to Import"
    files_group.Location = rt.dotNetObject("System.Drawing.Point", S(10), y)
    files_group.Size = rt.dotNetObject("System.Drawing.Size", S(570), S(160))
    files_group.ForeColor = Color.FromArgb(180, 200, 220)
    files_group.Font = bold_font
    form.Controls.Add(files_group)

    file_list = rt.dotNetObject("System.Windows.Forms.ListBox")
    file_list.Location = rt.dotNetObject("System.Drawing.Point", S(10), S(22))
    file_list.Size = rt.dotNetObject("System.Drawing.Size", S(440), S(125))
    file_list.BackColor = Color.FromArgb(30, 30, 33)
    file_list.ForeColor = Color.FromArgb(200, 200, 200)
    file_list.Font = small_font
    file_list.SelectionMode = SelectionMode.MultiExtended
    file_list.BorderStyle = BorderStyle_enum.FixedSingle
    files_group.Controls.Add(file_list)

    def make_btn(parent, text, x, yy, w=S(110), h=S(28)):
        b = rt.dotNetObject("System.Windows.Forms.Button")
        b.Text = text
        b.Location = rt.dotNetObject("System.Drawing.Point", x, yy)
        b.Size = rt.dotNetObject("System.Drawing.Size", w, h)
        b.BackColor = Color.FromArgb(60, 60, 65)
        b.ForeColor = Color.FromArgb(220, 220, 220)
        b.FlatStyle = FlatStyle.Flat
        b.Font = small_font
        parent.Controls.Add(b)
        return b

    btn_add = make_btn(files_group, "Add Files", S(455), S(22))
    btn_folder = make_btn(files_group, "Add Folder", S(455), S(55))
    btn_remove = make_btn(files_group, "Remove", S(455), S(88))
    btn_clear = make_btn(files_group, "Clear", S(455), S(121))

    y += S(170)

    # ---- ORIENTATION & SCALE ----
    orient_group = rt.dotNetObject("System.Windows.Forms.GroupBox")
    orient_group.Text = "Orientation && Scale"
    orient_group.Location = rt.dotNetObject("System.Drawing.Point", S(10), y)
    orient_group.Size = rt.dotNetObject("System.Drawing.Size", S(570), S(55))
    orient_group.ForeColor = Color.FromArgb(180, 200, 220)
    orient_group.Font = bold_font
    form.Controls.Add(orient_group)

    def make_label(parent, text, x, yy):
        lbl = rt.dotNetObject("System.Windows.Forms.Label")
        lbl.Text = text
        lbl.Location = rt.dotNetObject("System.Drawing.Point", x, yy)
        lbl.AutoSize = True
        lbl.Font = small_font
        lbl.ForeColor = Color.FromArgb(200, 200, 200)
        parent.Controls.Add(lbl)
        return lbl

    def make_combo(parent, items, x, yy, w=S(100)):
        cmb = rt.dotNetObject("System.Windows.Forms.ComboBox")
        cmb.Location = rt.dotNetObject("System.Drawing.Point", x, yy)
        cmb.Size = rt.dotNetObject("System.Drawing.Size", w, S(22))
        cmb.DropDownStyle = ComboBoxStyle.DropDownList
        cmb.BackColor = Color.FromArgb(30, 30, 33)
        cmb.ForeColor = Color.FromArgb(200, 200, 200)
        cmb.Font = small_font
        for item in items:
            cmb.Items.Add(item)
        cmb.SelectedIndex = 0
        parent.Controls.Add(cmb)
        return cmb

    make_label(orient_group, "Up Axis:", S(10), S(25))
    cmb_up = make_combo(orient_group, ["Y (GLTF)", "Z (CAD/Max)", "X"], S(80), S(22), S(110))

    make_label(orient_group, "Scale:", S(205), S(25))
    cmb_scale = make_combo(orient_group, ["Meters (1.0)", "Centimeters (100)", "Inches (39.37)", "Custom"], S(255), S(22), S(150))

    txt_custom = rt.dotNetObject("System.Windows.Forms.TextBox")
    txt_custom.Location = rt.dotNetObject("System.Drawing.Point", S(415), S(22))
    txt_custom.Size = rt.dotNetObject("System.Drawing.Size", S(70), S(22))
    txt_custom.BackColor = Color.FromArgb(30, 30, 33)
    txt_custom.ForeColor = Color.FromArgb(200, 200, 200)
    txt_custom.Font = small_font
    txt_custom.Text = "1.0"
    txt_custom.Enabled = False
    orient_group.Controls.Add(txt_custom)

    y += S(65)

    # ---- MESH OPTIONS ----
    mesh_group = rt.dotNetObject("System.Windows.Forms.GroupBox")
    mesh_group.Text = "Mesh Options"
    mesh_group.Location = rt.dotNetObject("System.Drawing.Point", S(10), y)
    mesh_group.Size = rt.dotNetObject("System.Drawing.Size", S(570), S(85))
    mesh_group.ForeColor = Color.FromArgb(180, 200, 220)
    mesh_group.Font = bold_font
    form.Controls.Add(mesh_group)

    def make_check(parent, text, x, yy, checked=False):
        chk = rt.dotNetObject("System.Windows.Forms.CheckBox")
        chk.Text = text
        chk.Location = rt.dotNetObject("System.Drawing.Point", x, yy)
        chk.AutoSize = True
        chk.Font = small_font
        chk.ForeColor = Color.FromArgb(200, 200, 200)
        chk.Checked = checked
        parent.Controls.Add(chk)
        return chk

    chk_weld = make_check(mesh_group, "Weld Vertices", S(10), S(22))
    txt_weld = rt.dotNetObject("System.Windows.Forms.TextBox")
    txt_weld.Location = rt.dotNetObject("System.Drawing.Point", S(145), S(22))
    txt_weld.Size = rt.dotNetObject("System.Drawing.Size", S(55), S(20))
    txt_weld.BackColor = Color.FromArgb(30, 30, 33)
    txt_weld.ForeColor = Color.FromArgb(200, 200, 200)
    txt_weld.Font = small_font
    txt_weld.Text = "0.001"
    mesh_group.Controls.Add(txt_weld)

    chk_flip = make_check(mesh_group, "Flip Normals", S(220), S(22))

    make_label(mesh_group, "Topology:", S(350), S(25))
    cmb_topology = make_combo(mesh_group, ["Triangles", "Quads", "N-Gons"], S(420), S(22), S(130))
    cmb_topology.SelectedIndex = 1  # Default to Quads

    chk_smooth = make_check(mesh_group, "Auto-Smooth", S(10), S(55), checked=True)
    chk_degen = make_check(mesh_group, "Remove Degenerates", S(150), S(55), checked=True)

    y += S(90)

    # ---- MATERIAL & TEXTURE OPTIONS ----
    mat_group = rt.dotNetObject("System.Windows.Forms.GroupBox")
    mat_group.Text = "Materials && Textures"
    mat_group.Location = rt.dotNetObject("System.Drawing.Point", S(10), y)
    mat_group.Size = rt.dotNetObject("System.Drawing.Size", S(570), S(80))
    mat_group.ForeColor = Color.FromArgb(180, 200, 220)
    mat_group.Font = bold_font
    form.Controls.Add(mat_group)

    chk_mats = make_check(mat_group, "Import Materials", S(10), S(22), checked=True)
    chk_tex = make_check(mat_group, "Import Textures", S(170), S(22), checked=True)
    chk_hier = make_check(mat_group, "Preserve Hierarchy", S(330), S(22), checked=True)

    make_label(mat_group, "Renderer:", S(10), S(52))
    cmb_renderer = make_combo(mat_group, ["Physical (Default)", "V-Ray", "Corona", "Arnold"], S(90), S(49), S(155))

    make_label(mat_group, "Texture Folder:", S(260), S(52))
    txt_tex = rt.dotNetObject("System.Windows.Forms.TextBox")
    txt_tex.Location = rt.dotNetObject("System.Drawing.Point", S(370), S(49))
    txt_tex.Size = rt.dotNetObject("System.Drawing.Size", S(190), S(22))
    txt_tex.BackColor = Color.FromArgb(30, 30, 33)
    txt_tex.ForeColor = Color.FromArgb(200, 200, 200)
    txt_tex.Font = small_font
    txt_tex.Text = ""
    mat_group.Controls.Add(txt_tex)

    y += S(90)

    # ---- LOG ----
    log_group = rt.dotNetObject("System.Windows.Forms.GroupBox")
    log_group.Text = "Import Log"
    log_group.Location = rt.dotNetObject("System.Drawing.Point", S(10), y)
    log_group.Size = rt.dotNetObject("System.Drawing.Size", S(570), S(200))
    log_group.ForeColor = Color.FromArgb(180, 200, 220)
    log_group.Font = bold_font
    form.Controls.Add(log_group)

    txt_log = rt.dotNetObject("System.Windows.Forms.TextBox")
    txt_log.Location = rt.dotNetObject("System.Drawing.Point", S(10), S(22))
    txt_log.Size = rt.dotNetObject("System.Drawing.Size", S(550), S(168))
    txt_log.BackColor = Color.FromArgb(20, 20, 22)
    txt_log.ForeColor = Color.FromArgb(180, 220, 180)
    txt_log.Font = rt.dotNetObject("System.Drawing.Font", "Consolas", 8.0)
    txt_log.Multiline = True
    txt_log.ReadOnly = True
    txt_log.ScrollBars = ScrollBars.Vertical
    txt_log.BorderStyle = BorderStyle_enum.FixedSingle
    log_group.Controls.Add(txt_log)

    y += S(210)

    # ---- STATUS ----
    lbl_status = rt.dotNetObject("System.Windows.Forms.Label")
    lbl_status.Text = "Ready"
    lbl_status.Location = rt.dotNetObject("System.Drawing.Point", S(10), y)
    lbl_status.Size = rt.dotNetObject("System.Drawing.Size", S(350), S(22))
    lbl_status.Font = bold_font
    lbl_status.ForeColor = Color.FromArgb(120, 220, 140)
    form.Controls.Add(lbl_status)

    # ---- BUTTONS ----
    btn_import = rt.dotNetObject("System.Windows.Forms.Button")
    btn_import.Text = "IMPORT"
    btn_import.Location = rt.dotNetObject("System.Drawing.Point", S(400), y)
    btn_import.Size = rt.dotNetObject("System.Drawing.Size", S(100), S(32))
    btn_import.BackColor = Color.FromArgb(40, 120, 50)
    btn_import.ForeColor = Color.FromArgb(255, 255, 255)
    btn_import.FlatStyle = FlatStyle.Flat
    btn_import.Font = bold_font
    form.Controls.Add(btn_import)

    btn_close = rt.dotNetObject("System.Windows.Forms.Button")
    btn_close.Text = "Close"
    btn_close.Location = rt.dotNetObject("System.Drawing.Point", S(510), y)
    btn_close.Size = rt.dotNetObject("System.Drawing.Size", S(70), S(32))
    btn_close.BackColor = Color.FromArgb(60, 60, 65)
    btn_close.ForeColor = Color.FromArgb(220, 220, 220)
    btn_close.FlatStyle = FlatStyle.Flat
    btn_close.Font = small_font
    form.Controls.Add(btn_close)

    # ---- EVENT HANDLERS ----
    def on_add_files(*args):
        dlg = rt.dotNetObject("System.Windows.Forms.OpenFileDialog")
        dlg.Filter = "GLTF Files (*.gltf;*.glb)|*.gltf;*.glb|All Files (*.*)|*.*"
        dlg.Multiselect = True
        dlg.Title = "Select GLTF/GLB Files"
        dlg.ShowDialog()
        try:
            names = dlg.FileNames
            count = names.Count if hasattr(names, 'Count') else len(names)
            for i in range(count):
                fp_str = str(names[i])
                if fp_str and fp_str not in _file_paths:
                    _file_paths.append(fp_str)
                    file_list.Items.Add(os.path.basename(fp_str))
        except Exception:
            # Single file fallback
            fp_str = str(dlg.FileName)
            if fp_str and fp_str not in _file_paths:
                _file_paths.append(fp_str)
                file_list.Items.Add(os.path.basename(fp_str))

    def on_add_folder(*args):
        dlg = rt.dotNetObject("System.Windows.Forms.FolderBrowserDialog")
        dlg.Description = "Select folder containing GLTF/GLB files"
        dlg.ShowDialog()
        try:
            folder = str(dlg.SelectedPath)
            if folder and os.path.isdir(folder):
                for fn in os.listdir(folder):
                    if fn.lower().endswith(('.gltf', '.glb')):
                        fp = os.path.join(folder, fn)
                        if fp not in _file_paths:
                            _file_paths.append(fp)
                            file_list.Items.Add(fn)
        except Exception:
            pass

    def on_remove(*args):
        selected = []
        for i in range(file_list.SelectedIndices.Count):
            selected.append(file_list.SelectedIndices.Item[i])
        for idx in sorted(selected, reverse=True):
            _file_paths.pop(idx)
            file_list.Items.RemoveAt(idx)

    def on_clear(*args):
        _file_paths.clear()
        file_list.Items.Clear()

    def on_scale_change(*args):
        txt_custom.Enabled = (cmb_scale.SelectedIndex == 3)

    def on_import(*args):
        count = len(_file_paths)
        if count == 0:
            txt_log.Text = "No files to import. Add files first."
            return

        opts = ImportOptions()
        opts.up_axis = ['Y', 'Z', 'X'][cmb_up.SelectedIndex]
        opts.scale_preset = ['meters', 'centimeters', 'inches', 'custom'][cmb_scale.SelectedIndex]
        if cmb_scale.SelectedIndex == 3:
            try:
                opts.scale = float(str(txt_custom.Text))
            except ValueError:
                opts.scale = 1.0
        opts.weld_vertices = chk_weld.Checked
        try:
            opts.weld_threshold = float(str(txt_weld.Text))
        except ValueError:
            opts.weld_threshold = 0.001
        opts.flip_normals = chk_flip.Checked
        opts.remove_degenerates = chk_degen.Checked
        opts.auto_smooth = chk_smooth.Checked

        # Topology selection
        topology_map = ['triangles', 'quads', 'ngons']
        opts.topology = topology_map[cmb_topology.SelectedIndex]

        opts.import_materials = chk_mats.Checked
        opts.import_textures = chk_tex.Checked
        opts.import_hierarchy = chk_hier.Checked

        # Renderer selection
        renderer_map = ['physical', 'vray', 'corona', 'arnold']
        opts.renderer = renderer_map[cmb_renderer.SelectedIndex]

        tex_folder = str(txt_tex.Text).strip()
        opts.texture_folder = tex_folder if tex_folder else ""

        filepaths = list(_file_paths)

        lbl_status.Text = f"Importing {count} file(s)..."
        lbl_status.ForeColor = Color.FromArgb(220, 200, 120)
        txt_log.Text = ""
        form.Refresh()

        try:
            log = ImportLog()
            log.set_ui(txt_log)
            all_created, log = import_batch(filepaths, opts=opts, log=log)

            if log.error_count > 0:
                lbl_status.Text = f"Done with {log.error_count} error(s). {len(all_created)} objects."
                lbl_status.ForeColor = Color.FromArgb(220, 80, 80)
            elif log.warning_count > 0:
                lbl_status.Text = f"Done with {log.warning_count} warning(s). {len(all_created)} objects."
                lbl_status.ForeColor = Color.FromArgb(220, 200, 120)
            else:
                lbl_status.Text = f"Import complete. {len(all_created)} objects created."
                lbl_status.ForeColor = Color.FromArgb(120, 220, 140)
        except Exception as e:
            txt_log.AppendText(f"\r\nFATAL ERROR:\r\n{traceback.format_exc()}")
            lbl_status.Text = "Import failed with errors."
            lbl_status.ForeColor = Color.FromArgb(220, 80, 80)

    def on_close(*args):
        form.Close()

    # Wire up events
    rt.dotNet.addEventHandler(btn_add, "Click", on_add_files)
    rt.dotNet.addEventHandler(btn_folder, "Click", on_add_folder)
    rt.dotNet.addEventHandler(btn_remove, "Click", on_remove)
    rt.dotNet.addEventHandler(btn_clear, "Click", on_clear)
    rt.dotNet.addEventHandler(cmb_scale, "SelectedIndexChanged", on_scale_change)
    rt.dotNet.addEventHandler(btn_import, "Click", on_import)
    rt.dotNet.addEventHandler(btn_close, "Click", on_close)

    # Show and parent to Max window
    form.Show()
    try:
        import ctypes
        GWL_HWNDPARENT = -8
        max_hwnd = int(str(rt.windows.getMaxHWND()))
        form_handle = int(str(form.Handle))
        ctypes.windll.user32.SetWindowLongPtrW(form_handle, GWL_HWNDPARENT, max_hwnd)
    except Exception:
        # Fallback: just keep it floating
        pass


# ============================================================================
# LEGACY API
# ============================================================================

def import_dialog(scale=1.0):
    """Open the full UI. Legacy compatibility."""
    show_ui()


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    if HAS_MAX:
        show_ui()
    else:
        print("Running in parse-only mode (no 3ds Max).")
        print("Usage: import_file('path/to/model.gltf', scale=1.0)")
