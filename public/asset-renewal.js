/* Extends the standard CI metadata form without mixing subscription renewal with hardware warranty. */
(() => {
  const warrantyInput = document.getElementById("assetWarrantyEnd");
  warrantyInput.closest("label").firstChild.textContent = "Warranty end ";
  const renewal = document.createElement("label");
  renewal.innerHTML = 'Subscription / licence renewal <small>Software, SaaS or contract renewal</small><input id="assetRenewalDate" type="date">';
  warrantyInput.closest(".two").append(renewal);

  const originalMetadataValues = metadataValues;
  metadataValues = () => ({...originalMetadataValues(), renewalDate: assetRenewalDate.value});

  const originalApplyAssetMetadata = applyAssetMetadata;
  applyAssetMetadata = (metadata = {}) => {
    originalApplyAssetMetadata(metadata);
    assetRenewalDate.value = metadata.renewalDate || "";
  };
})();
