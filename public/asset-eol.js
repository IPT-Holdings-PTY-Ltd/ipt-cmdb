/* Keeps end-of-life distinct from contract renewal and warranty milestones. */
(() => {
  const eol = document.createElement("label");
  eol.innerHTML = 'End-of-life date <small>Vendor support/end-of-support milestone</small><input id="assetEndOfLifeDate" type="date">';
  document.getElementById("assetRenewalDate").closest(".two").append(eol);

  const originalMetadataValues = metadataValues;
  metadataValues = () => ({...originalMetadataValues(), endOfLifeDate: assetEndOfLifeDate.value});

  const originalApplyAssetMetadata = applyAssetMetadata;
  applyAssetMetadata = (metadata = {}) => {
    originalApplyAssetMetadata(metadata);
    assetEndOfLifeDate.value = metadata.endOfLifeDate || "";
  };
})();
