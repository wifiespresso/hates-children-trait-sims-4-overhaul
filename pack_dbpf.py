import os
import zlib
import struct

def parse_key(filename):
    # e.g. E882D22F!00000000!0000000000F01035.mixer.xml
    base = filename
    while '.' in base:
        base = base.rsplit('.', 1)[0]
    parts = base.split('!')
    if len(parts) < 3: return None
    try:
        t = int(parts[0], 16)
        g = int(parts[1], 16)
        i = parts[2].zfill(16)
        i_hi = int(i[:8], 16)
        i_lo = int(i[8:], 16)
        return (t, g, i_hi, i_lo)
    except:
        return None

resources = []
folder = r"C:\Users\marisa\Downloads\mod tests\HatesChildren_Overhaul_Standalone"
bin_dir = r"C:\Users\marisa\Downloads\mod tests\HC_BinaryResources"

for fname in os.listdir(folder):
    if fname.endswith('.xml'):
        key = parse_key(fname)
        if key:
            with open(os.path.join(folder, fname), 'rb') as f:
                data = f.read()
            resources.append((key, data))

for fname in os.listdir(bin_dir):
    key = parse_key(fname)
    if key:
        with open(os.path.join(bin_dir, fname), 'rb') as f:
            data = f.read()
        resources.append((key, data))

print(f"Loaded {len(resources)} resources.")

out_path = r"C:\Users\marisa\Documents\Electronic Arts\The Sims 4\Mods\Hates Children! Mod\HatesChildren_Overhaul.package"

with open(out_path, 'wb') as out:
    out.write(b'\x00' * 96) # Reserve header
    
    index = []
    for key, data in resources:
        offset = out.tell()
        # Compress with zlib
        compressed = zlib.compress(data)
        out.write(compressed)
        index.append((key, offset, len(compressed), len(data)))
    
    idx_start = out.tell()
    # Write index constant type header (0)
    out.write(struct.pack('<I', 0))
    
    for key, offset, comp_sz, decomp_sz in index:
        out.write(struct.pack('<IIII', key[0], key[1], key[2], key[3]))
        out.write(struct.pack('<I', offset))
        # Size | 0x80000000 for compressed
        out.write(struct.pack('<I', comp_sz | 0x80000000))
        out.write(struct.pack('<I', decomp_sz))
        out.write(struct.pack('<H', 0x5A42)) # ZLIB
        out.write(struct.pack('<H', 1)) # Committed
        
    idx_sz = out.tell() - idx_start
    
    # Write header
    out.seek(0)
    out.write(b'DBPF')
    out.write(struct.pack('<II', 2, 1))
    out.write(b'\x00' * 24)
    out.write(struct.pack('<I', len(resources)))
    out.write(struct.pack('<I', 0)) # idx sz on disk
    out.write(struct.pack('<I', idx_sz)) # idx sz in memory
    out.write(b'\x00' * 12)
    out.write(struct.pack('<II', 3, idx_start))

print("Package built successfully with standard ZLIB compression!")
