#!/usr/bin/env python3
"""Bound and check the mmap of a GpuPool-resident blob.

Three defects on the guest side of the MAP_INFO_POOL contract, all in
virtio_gpu/, all reachable only for a blob the host answered with
VIRTIO_GPU_MAP_INFO_POOL:

  1. virtio_gpu_vram_mmap() bounds the mapping against vram->vram_node.size.
     For a host-pool blob that node is the BAR reservation drm_mm made when
     the blob was created -- ALIGN(params->size, SZ_2M) -- not the blob.  A
     1,060,864-byte ASG ring therefore accepts an mmap of up to 2 MiB, and the
     bytes past 0x103000 belong to whatever the pool allocated next.

  2. The pool branch maps gpu_pool_base + pool_offset with no check that the
     range is inside the pool at all.  pool_offset arrives from the host as a
     32-bit byte offset; if a pool ever exceeds 4 GiB, or the host sends an
     offset the guest cannot represent, the guest silently remaps a wrapped or
     out-of-range IPA -- unrelated memory rather than a refused mmap.

  3. virtio_gpu_vram_map_dma_buf() describes the BAR slot for every vram
     object, including pool-resident ones, whose bytes are not in the BAR at
     all.  Nothing imports a pool blob's dma-buf today (scanout refuses vram
     objects, virtgpu_plane.c), so this is a trap rather than a live bug; it
     still returns a window the blob never occupied.

The patch adds gpu_pool_size (from the same DT resource the base comes from)
and blob_size (the mmap bound), validates, and traces the mapping so the
guest's view of pool_offset is visible in dmesg.

Usage:
    python scripts/patch_pool_mmap_bounds.py --root <tree with virtio_gpu/>
    python scripts/patch_pool_mmap_bounds.py --root <tree> --check

--check verifies every anchor is present exactly once and reports what would
change, without writing.  Idempotent: a second run detects the applied form and
reports it instead of failing.
"""

import argparse
import hashlib
import sys
from pathlib import Path

MARKER = 'gpu_pool_size'


def replace_once(text, old, new, what):
    n = text.count(old)
    if n != 1:
        raise SystemExit(f'anchor not unique ({n} hits) for {what}')
    return text.replace(old, new)


# --- virtio_gpu/virtgpu_drv.h -------------------------------------------------

DRV_POOL_BASE_OLD = '''	phys_addr_t gpu_pool_base;
'''
DRV_POOL_BASE_NEW = '''	phys_addr_t gpu_pool_base;
	/* Size of the pool window the base above came from (same DT resource), 0 when
	 * unknown. Only used to refuse a pool offset the host could not have meant. */
	phys_addr_t gpu_pool_size;
'''

DRV_VRAM_OLD = '''	bool pool_resident;
	u64 pool_offset;
'''
DRV_VRAM_NEW = '''	bool pool_resident;
	u64 pool_offset;
	/* Size of the blob as the guest asked for it (PAGE_ALIGN'd). This -- not
	 * vram_node.size, which is the BAR reservation ALIGN(size, SZ_2M) -- is the
	 * bound a pool-resident mmap gets. */
	u64 blob_size;
'''


# --- virtio_gpu/virtgpu_kms.c -------------------------------------------------

KMS_NAMED_OLD = '''static phys_addr_t virtio_gpu_find_pool_base_named(const char *prefix)
{
	struct device_node *rmem, *child;
	phys_addr_t base = 0;

	rmem = of_find_node_by_path("/reserved-memory");
	if (!rmem)
		return 0;
	for_each_child_of_node(rmem, child) {
		struct resource res;

		if (!of_node_name_prefix(child, prefix))
			continue;
		if (of_address_to_resource(child, 0, &res) == 0) {
			base = res.start;
			of_node_put(child);
			break;
		}
	}
	of_node_put(rmem);
	return base;
}
'''
KMS_NAMED_NEW = '''static phys_addr_t virtio_gpu_find_pool_base_named(const char *prefix,
						  phys_addr_t *size_out)
{
	struct device_node *rmem, *child;
	phys_addr_t base = 0;

	*size_out = 0;
	rmem = of_find_node_by_path("/reserved-memory");
	if (!rmem)
		return 0;
	for_each_child_of_node(rmem, child) {
		struct resource res;

		if (!of_node_name_prefix(child, prefix))
			continue;
		if (of_address_to_resource(child, 0, &res) == 0) {
			base = res.start;
			*size_out = resource_size(&res);
			of_node_put(child);
			break;
		}
	}
	of_node_put(rmem);
	return base;
}
'''

KMS_FIND_OLD = '''static phys_addr_t virtio_gpu_find_pool_base(const char **which)
{
	phys_addr_t base = virtio_gpu_find_pool_base_named("gfx_host");

	*which = "gfx_host";
	if (!base) {
		base = virtio_gpu_find_pool_base_named("drm2kgsl_host");
		*which = "drm2kgsl_host";
	}
	if (!base) {
		/* venus (vkr) host-allocated transport shmems, same
		 * pool-resident MAP_INFO_POOL contract as the other two
		 */
		base = virtio_gpu_find_pool_base_named("venus_host");
		*which = "venus_host";
	}
	return base;
}
'''
KMS_FIND_NEW = '''static phys_addr_t virtio_gpu_find_pool_base(const char **which,
					     phys_addr_t *size_out)
{
	phys_addr_t base = virtio_gpu_find_pool_base_named("gfx_host", size_out);

	*which = "gfx_host";
	if (!base) {
		base = virtio_gpu_find_pool_base_named("drm2kgsl_host", size_out);
		*which = "drm2kgsl_host";
	}
	if (!base) {
		/* venus (vkr) host-allocated transport shmems, same
		 * pool-resident MAP_INFO_POOL contract as the other two
		 */
		base = virtio_gpu_find_pool_base_named("venus_host", size_out);
		*which = "venus_host";
	}
	return base;
}
'''

KMS_CALL_OLD = '''	vgdev->gpu_pool_base = virtio_gpu_find_pool_base(&pool_node);
	if (vgdev->gpu_pool_base)
		DRM_INFO("host pool: %s base %pa\\n", pool_node, &vgdev->gpu_pool_base);
'''
KMS_CALL_NEW = '''	vgdev->gpu_pool_base = virtio_gpu_find_pool_base(&pool_node,
							&vgdev->gpu_pool_size);
	if (vgdev->gpu_pool_base)
		DRM_INFO("host pool: %s base %pa +0x%llx\\n", pool_node,
			 &vgdev->gpu_pool_base,
			 (unsigned long long)vgdev->gpu_pool_size);
'''


# --- virtio_gpu/virtgpu_vram.c ------------------------------------------------

VRAM_BOUND_OLD = '''	if (vm_end > vram->vram_node.size)
		return -EINVAL;
'''
VRAM_BOUND_NEW = '''	/*
	 * For a pool-resident blob vram_node.size is the BAR reservation the create
	 * path made -- ALIGN(size, SZ_2M) -- not the blob, so it would accept a
	 * mapping up to 2 MiB long for a 1 MiB blob and let the tail alias whatever
	 * the pool handed out next. Bound by the size the blob was created with when
	 * we know it.
	 */
	if (vm_end > (vram->blob_size ? vram->blob_size : vram->vram_node.size))
		return -EINVAL;
'''

VRAM_POOL_OLD = '''	if (vram->pool_resident) {
		phys_addr_t pa;

		if (!vgdev->gpu_pool_base) {
			pr_err("virtio-gpu: pool-resident blob but no pool base in DT\\n");
			return -EINVAL;
		}
		pa = vgdev->gpu_pool_base + vram->pool_offset;
		return io_remap_pfn_range(vma, vma->vm_start,
					  (pa >> PAGE_SHIFT) + vma->vm_pgoff,
					  vm_size, vma->vm_page_prot);
	}
'''
VRAM_POOL_NEW = '''	if (vram->pool_resident) {
		u64 pool_off = vram->pool_offset +
			       ((u64)vma->vm_pgoff << PAGE_SHIFT);
		phys_addr_t pa;

		if (!vgdev->gpu_pool_base) {
			pr_err("virtio-gpu: pool-resident blob but no pool base in DT\\n");
			return -EINVAL;
		}
		/*
		 * The offset is the host's to choose and reaches us as a 32-bit byte
		 * offset. Check it against the pool window we were told about rather
		 * than remapping whatever IPA falls out of a wrapped or oversized
		 * value: an out-of-range offset is a host/guest contract violation,
		 * and silently mapping unrelated memory over it is the worst possible
		 * reading of it.
		 */
		if ((vram->pool_offset & ~PAGE_MASK) ||
		    (vgdev->gpu_pool_size &&
		     (pool_off > vgdev->gpu_pool_size ||
		      vm_size > vgdev->gpu_pool_size - pool_off))) {
			pr_err("virtio-gpu: pool mmap out of range: res=%u off=0x%llx pgoff=%lu size=%lu pool=0x%llx+0x%llx\\n",
			       bo->hw_res_handle,
			       (unsigned long long)vram->pool_offset,
			       vma->vm_pgoff, vm_size,
			       (unsigned long long)vgdev->gpu_pool_base,
			       (unsigned long long)vgdev->gpu_pool_size);
			return -EINVAL;
		}
		pa = vgdev->gpu_pool_base + pool_off;
		/* One line per pool mapping: the only guest-side record of which pool
		 * bytes a resource's CPU mapping points at, and what the host's
		 * map_blob offset was. */
		pr_info("virtio-gpu: pool mmap res=%u off=0x%llx size=%lu\\n",
			bo->hw_res_handle, (unsigned long long)pool_off, vm_size);
		return io_remap_pfn_range(vma, vma->vm_start, pa >> PAGE_SHIFT,
					  vm_size, vma->vm_page_prot);
	}
'''

VRAM_CREATE_OLD = '''	params->size = PAGE_ALIGN(params->size);
	drm_gem_private_object_init(vgdev->ddev, obj, params->size);

	/* Create fake offset */
'''
VRAM_CREATE_NEW = '''	params->size = PAGE_ALIGN(params->size);
	vram->blob_size = params->size;
	drm_gem_private_object_init(vgdev->ddev, obj, params->size);

	/* Create fake offset */
'''

VRAM_GUESTPOOL_OLD = '''	vram->map_info = VIRTIO_GPU_MAP_CACHE_CACHED;
	vram->vram_node.size = params->size;      /* mmap bounds check only (no drm_mm insert) */
'''
VRAM_GUESTPOOL_NEW = '''	vram->map_info = VIRTIO_GPU_MAP_CACHE_CACHED;
	vram->blob_size = params->size;
	vram->vram_node.size = params->size;      /* mmap bounds check only (no drm_mm insert) */
'''

VRAM_DMABUF_OLD = '''	if (vram->guest_pool_owned) {
		/* Guest-pool blobs are scatter-backed. Mapping vram_node.start here would map
'''
VRAM_DMABUF_NEW = '''	if (vram->pool_resident && !vram->guest_pool_owned) {
		/*
		 * A host-pool blob's bytes are at gpu_pool_base + pool_offset, not in the
		 * BAR slot vram_node describes (that slot exists only to carry the offset
		 * in the map_blob request). Exporting it as the BAR window would hand the
		 * importer a region the blob never occupied -- a second physical window
		 * for one resource. Refuse instead; nothing imports a pool blob's dma-buf
		 * today (virtio_drm_get_scanout_buffer() rejects vram objects).
		 */
		pr_err_once("virtio-gpu: refusing dma-buf export of pool-resident blob res=%u off=0x%llx\\n",
			    bo->hw_res_handle, (unsigned long long)vram->pool_offset);
		ret = -EOPNOTSUPP;
		goto out;
	}

	if (vram->guest_pool_owned) {
		/* Guest-pool blobs are scatter-backed. Mapping vram_node.start here would map
'''


def patch_drv(text):
    if 'u64 blob_size;' in text and MARKER in text:
        return text, False
    text = replace_once(text, DRV_POOL_BASE_OLD, DRV_POOL_BASE_NEW, 'drv.h gpu_pool_base')
    text = replace_once(text, DRV_VRAM_OLD, DRV_VRAM_NEW, 'drv.h pool_offset')
    return text, True


def patch_kms(text):
    if 'phys_addr_t *size_out' in text:
        return text, False
    text = replace_once(text, KMS_NAMED_OLD, KMS_NAMED_NEW, 'kms.c find_pool_base_named')
    text = replace_once(text, KMS_FIND_OLD, KMS_FIND_NEW, 'kms.c find_pool_base')
    text = replace_once(text, KMS_CALL_OLD, KMS_CALL_NEW, 'kms.c find_pool_base call')
    return text, True


def patch_vram(text):
    if 'virtio-gpu: pool mmap res=' in text:
        return text, False
    text = replace_once(text, VRAM_BOUND_OLD, VRAM_BOUND_NEW, 'vram.c mmap bound')
    text = replace_once(text, VRAM_POOL_OLD, VRAM_POOL_NEW, 'vram.c pool branch')
    text = replace_once(text, VRAM_CREATE_OLD, VRAM_CREATE_NEW, 'vram.c vram_create blob_size')
    text = replace_once(text, VRAM_GUESTPOOL_OLD, VRAM_GUESTPOOL_NEW,
                        'vram.c guest_pool_create blob_size')
    text = replace_once(text, VRAM_DMABUF_OLD, VRAM_DMABUF_NEW, 'vram.c map_dma_buf guard')
    return text, True


TARGETS = [
    ('virtio_gpu/virtgpu_drv.h', patch_drv),
    ('virtio_gpu/virtgpu_kms.c', patch_kms),
    ('virtio_gpu/virtgpu_vram.c', patch_vram),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', default='.', help='tree containing virtio_gpu/')
    ap.add_argument('--check', action='store_true',
                    help='verify anchors and report, write nothing')
    args = ap.parse_args()
    root = Path(args.root)

    changes = {}
    for rel, fn in TARGETS:
        p = root / rel
        if not p.is_file():
            raise SystemExit(f'missing {p}')
        raw = p.read_text(encoding='utf-8')
        new, changed = fn(raw)
        state = 'patched' if changed else 'already applied'
        print(f'{rel}: {state}')
        if changed:
            changes[str(p)] = hashlib.sha256(new.encode('utf-8')).hexdigest()
            if not args.check:
                p.write_text(new, encoding='utf-8')

    if args.check:
        print('check only, nothing written')
    else:
        for path, digest in changes.items():
            print('sha256', digest, path)


if __name__ == '__main__':
    main()
