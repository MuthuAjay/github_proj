import struct
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from azure.storage.blob import BlobServiceClient

# --- CONFIGURE YOUR AZURE ACCESS ---
CONNECTION_STRING = "your_azure_storage_connection_string"
CONTAINER_NAME = "your-container-name"

# .idx downloads are network-bound (one small blob fetch each) - a thread
# pool overlaps their latency instead of paying it one download at a time.
MAX_WORKERS = 16

HASH_LEN = 20   # SHA-1. See the guard in parse_idx_bytes() for why this
                # can't just be swapped to 32 to "support" SHA-256.


def parse_idx_bytes(idx_bytes, label=""):
    """Parses a Git IDX V2 byte stream directly out of Azure memory.

    Only handles SHA-1 (20-byte hash) idx files. The fan-out table and the
    total-object count are hash-length-independent, so a SHA-256 idx would
    otherwise be misread with NO error at all: the SHA table read would
    silently desynchronize (each real 32-byte hash split across two bogus
    20-byte reads), corrupting every hash from the very first entry onward
    and producing a wrong-but-plausible-looking result. The layout check
    below catches that by verifying how much data trails the SHA-1-sized
    hash table against what a real SHA-1 v2 idx must have there (a CRC32
    + offset entry per object, plus the two trailing checksums) - a
    SHA-256 idx's true (32-byte) hash table would overrun into that region
    and blow the bounds check.
    """
    try:
        f = BytesIO(idx_bytes)
        magic = f.read(4)
        if magic != b'\xfftOc':  # Standard Git V2 Index Magic Header
            print(f"Error parsing {label}: not a git pack idx (bad magic)")
            return None
        version = struct.unpack('>I', f.read(4))[0]
        if version != 2:
            print(f"Error parsing {label}: idx version {version} unsupported "
                  f"(only v2 handled)")
            return None
        # Skip the first 255 entries of the fan-out table (255 * 4 bytes)
        f.seek(4 + 4 + (255 * 4))
        total_objects = struct.unpack('>I', f.read(4))[0]
        # Jump right past the full 256 entry fan-out table to the SHA table
        sha_table_start = 4 + 4 + (256 * 4)
        f.seek(sha_table_start)

        needed = total_objects * HASH_LEN
        if len(idx_bytes) - sha_table_start < needed:
            print(f"Error parsing {label}: truncated - needs {needed} bytes "
                  f"for {total_objects} SHA-1 hashes, only "
                  f"{len(idx_bytes) - sha_table_start} available")
            return None
        # What should trail the hash table in a genuine SHA-1 v2 idx: one
        # CRC32 (4B) + one offset entry (4B) per object, an optional large-
        # offset table (up to 8B extra per object, only for objects needing
        # a 64-bit offset), and two trailing 20-byte checksums.
        trailing = len(idx_bytes) - (sha_table_start + needed)
        min_trailing = total_objects * 8 + 40
        max_trailing = total_objects * 16 + 40
        if not (min_trailing <= trailing <= max_trailing):
            print(f"Error parsing {label}: idx layout doesn't match a SHA-1 "
                  f"v2 index ({trailing} trailing bytes, expected "
                  f"{min_trailing}-{max_trailing}) - likely a SHA-256 "
                  f"repository (unsupported) or a corrupted idx")
            return None

        hashes = set()
        for _ in range(total_objects):
            sha_bytes = f.read(HASH_LEN)
            hashes.add(sha_bytes.hex())
        return hashes
    except Exception as e:
        print(f"Error parsing idx bytes for {label}: {e}")
        return None


def _fetch_and_parse(container_client, name, info):
    idx_client = container_client.get_blob_client(info['idx_blob'])
    idx_bytes = idx_client.download_blob().readall()
    return name, parse_idx_bytes(idx_bytes, label=info['idx_blob'])
 
def audit_azure_packs():
    # Initialize Azure Client
    blob_service_client = BlobServiceClient.from_connection_string(CONNECTION_STRING)
    container_client = blob_service_client.get_container_client(CONTAINER_NAME)
    print(f"Scanning Azure Container '{CONTAINER_NAME}' for Git packs...")
    # Map out existing pack components in the storage account. list_blobs()
    # already returns each blob's size as part of its properties - grabbing
    # it here (once) means the earlier per-pack get_blob_properties() call
    # (one extra network round-trip per pack) isn't needed at all. Keyed by
    # name in a dict, not a list, so the pairing lookup below is O(1) per
    # blob instead of an O(n) scan repeated for every .idx found.
    all_blobs = {b.name: b for b in container_client.list_blobs()}
    pack_pairs = {}
    for blob_name, props in all_blobs.items():
        if blob_name.endswith('.idx'):
            base_name = blob_name[:-4]
            pack_name = base_name + '.pack'
            pack_props = all_blobs.get(pack_name)
            if pack_props is not None:
                pack_pairs[base_name] = {
                    'idx_blob': blob_name,
                    'pack_blob': pack_name,
                    'size_mb': pack_props.size / (1024 * 1024)
                }
    print(f"Found {len(pack_pairs)} valid Pack/IDX pairs on Azure.\n")

    # Download + parse every .idx concurrently - this is the network-bound
    # part (one small blob fetch each) and has no shared state, so threads
    # overlap the latency instead of paying it one download at a time.
    pack_objects_by_name = {}
    parse_failures = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_fetch_and_parse, container_client, name, info): name
                  for name, info in pack_pairs.items()}
        for fut in as_completed(futures):
            name = futures[fut]
            info = pack_pairs[name]
            try:
                _, pack_objects = fut.result()
            except Exception as e:
                print(f"Error downloading {info['idx_blob']}: {e}")
                pack_objects = None
            print(f"Streamed index data for: {info['idx_blob']} "
                  f"({info['size_mb']:.2f} MB pack associated)")
            if pack_objects is None:
                parse_failures.append(info['idx_blob'])
            else:
                pack_objects_by_name[name] = pack_objects

    # Evaluate object redundancy - this part is inherently sequential (each
    # pack's "is it redundant" verdict depends on everything decided before
    # it), but it's pure in-memory set arithmetic, no network waits, so it's
    # already fast; only the downloads above needed parallelizing.
    master_object_pool = set()
    unique_packs = []
    redundant_packs = []
    # Process largest packs first (optimizes the master pool layout)
    sorted_names = sorted(pack_objects_by_name,
                          key=lambda n: pack_pairs[n]['size_mb'], reverse=True)
    for name in sorted_names:
        info = pack_pairs[name]
        pack_objects = pack_objects_by_name[name]
        # Deduplication comparison logic
        new_objects = pack_objects - master_object_pool
        if len(new_objects) == 0:
            redundant_packs.append(info['pack_blob'])
        else:
            master_object_pool.update(new_objects)
            unique_packs.append((info['pack_blob'], len(new_objects)))

    # --- REPORT FINDINGS ---
    print("\n" + "="*60)
    print("📋 AZURE STORAGE DEDUPLICATION REPORT")
    print("="*60)
    print(f"✅ KEEP / ANALYZE THESE BLOBS (Contains unique/changed data):")
    for pack, new_count in unique_packs:
        print(f"  - {pack} (Contributes {new_count} unique code elements)")
    print(f"\n❌ SKIP THESE BLOBS (100% Redundant / No historical changes):")
    if not redundant_packs:
        print("  - None! All packs contain unique objects.")
    for pack in redundant_packs:
        print(f"  - {pack}")
    if parse_failures:
        print(f"\n⚠️  {len(parse_failures)} idx file(s) could NOT be parsed - "
              f"NOT included in the analysis above, so results may be "
              f"incomplete:")
        for blob in parse_failures:
            print(f"  - {blob}")
 
if __name__ == "__main__":
    audit_azure_packs()