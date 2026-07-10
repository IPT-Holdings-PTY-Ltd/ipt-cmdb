/* Root branding is deliberately MSP-scoped; customer overrides can be added later. */
(() => {
  const defaultWelcome = "Asset intelligence for your organisation.";

  function updatePreview() {
    const name = brandInput.value.trim() || "CMDB Hub";
    const logoText = (logoInput.value.trim() || "C").toUpperCase();
    const accent = accentInput.value || "#50d5b9";
    const email = brandSupportEmail.value.trim();
    const url = brandSupportUrl.value.trim();
    const welcome = brandWelcomeMessage.value.trim() || defaultWelcome;
    previewLogo.textContent = logoText;
    previewLogo.style.background = accent;
    previewName.textContent = name;
    previewWelcome.textContent = welcome;
    previewSupport.textContent = email || url || "Support details not configured";
  }

  const baseLoadBrand = loadBrand;
  loadBrand = async () => {
    await baseLoadBrand();
    if (!isRootContext()) return;
    const brand = await api("/api/branding?scope=msp");
    brandSupportEmail.value = brand.supportEmail || "";
    brandSupportUrl.value = brand.supportUrl || "";
    brandWelcomeMessage.value = brand.welcomeMessage || "";
    updatePreview();
  };

  saveBranding = async event => {
    event.preventDefault();
    try {
      await api("/api/branding", {method:"PUT", body:JSON.stringify({scope:"msp", name:brandInput.value, logoText:logoInput.value, accent:accentInput.value, supportEmail:brandSupportEmail.value, supportUrl:brandSupportUrl.value, welcomeMessage:brandWelcomeMessage.value})});
      await loadBrand();
    } catch (error) { alert(error.message); }
  };

  [brandInput, logoInput, accentInput, brandSupportEmail, brandSupportUrl, brandWelcomeMessage].forEach(input => input.addEventListener("input", updatePreview));
})();
