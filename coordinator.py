def active_bins(self, rvm_id: str) -> list[int]:
    """Return only bins that are really configured and in use.

    Strakke regels:
    - materiaal + api/config limit > 0  -> actief
    - materiaal + count > 0             -> actief
    - anders niet actief
    """
    active: list[int] = []

    for bin_no in range(1, 13):
        material = normalize_material(self.rvm_data(rvm_id).get(f"{BIN_MATERIAL_PREFIX}{bin_no}"))
        count = self._bin_count(rvm_id, bin_no)
        api_limit = self._bin_api_limit(rvm_id, bin_no)
        configured_limit = self.configured_bin_limit(rvm_id, bin_no)

        if material and ((api_limit > 0) or (configured_limit is not None and configured_limit > 0)):
            active.append(bin_no)
            continue

        if material and count > 0:
            active.append(bin_no)
            continue

    return active
