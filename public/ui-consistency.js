/* Shared UI language and scope behaviour. Loaded after feature-specific scripts. */
(() => {
  const roleName = role => ({platform_admin:"Platform admin", msp_operator:"MSP operator", client_reader:"Customer reader"}[role] || role);
  const renderAssetRows = () => {
    const query = assetSearch.value.trim().toLowerCase();
    const type = assetTypeFilter.value;
    const condition = assetConditionFilter.value;
    const rows = currentAssets.filter(asset => {
      const metadata = asset.metadata || {};
      const haystack = [asset.name, asset.type, asset.source, metadata.site, metadata.serviceOwner, metadata.technicalOwner].join(" ").toLowerCase();
      const owner = metadata.technicalOwner || metadata.serviceOwner;
      const needsAttention = ["warning", "critical", "offline"].includes(metadata.operationalStatus) || ["high", "critical"].includes(metadata.criticality);
      return (!query || haystack.includes(query)) && (!type || asset.type === type) && (!condition || (condition === "attention" && needsAttention) || (condition === "unassigned" && !owner));
    });
    assetRows.innerHTML = rows.map(row).join("") || '<tr><td colspan="7">No assets match these filters.</td></tr>';
    assetResultCount.textContent = `${rows.length} of ${currentAssets.length} assets`;
  };

  const baseLoadAssets = loadAssets;
  loadAssets = async () => {
    await baseLoadAssets();
    const labels = metrics.querySelectorAll("span");
    if (labels.length >= 3) { labels[0].textContent = "Assets"; labels[1].textContent = "Asset types"; labels[2].textContent = "Critical assets"; }
    const type = assetTypeFilter.value;
    assetTypeFilter.innerHTML = '<option value="">All types</option>' + [...new Set(currentAssets.map(asset => asset.type))].sort().map(item => `<option value="${esc(item)}">${esc(item)}</option>`).join("");
    assetTypeFilter.value = type;
    renderAssetRows();
  };
  [assetSearch, assetTypeFilter, assetConditionFilter].forEach(control => control.addEventListener("input", renderAssetRows));
  assetTypeFilter.addEventListener("change", renderAssetRows);
  assetConditionFilter.addEventListener("change", renderAssetRows);

  const baseLoadIntegrations = loadIntegrations;
  loadIntegrations = async () => {
    const list = await api("/api/integrations");
    integrationCards.innerHTML = list.map(integration => {
      const configured = integration.status !== "Not configured";
      return `<article class="integration-card ${configured ? "is-ready" : "is-pending"}"><h3>${esc(integration.name)}</h3><p class="integration-status">${configured ? "Ready to sync" : "Configuration required"}</p><small>${integration.lastSync ? "Last check: " + new Date(integration.lastSync).toLocaleString() : configured ? "Ready for a review-gated sync" : "Connection settings have not been supplied."}</small><button ${configured ? `onclick="runSync('${integration.type}')"` : "disabled title=\"Connection settings are not configured yet\""}>${configured ? "Test & sync" : "Not configured"}</button></article>`;
    }).join("");
  };

  const baseLoadDatabaseStatus = loadDatabaseStatus;
  loadDatabaseStatus = async () => {
    await baseLoadDatabaseStatus();
    const environmentManaged = databaseStatus.textContent.includes("environment");
    document.querySelector("#database form").classList.toggle("environment-managed", environmentManaged);
    document.querySelector(".database-actions button:last-child").textContent = environmentManaged ? "Environment-managed connection" : "Save and use PostgreSQL";
    document.querySelector(".database-actions button:last-child").disabled = environmentManaged;
    if (environmentManaged && !document.getElementById("environmentDatabaseNotice")) {
      databaseStatus.insertAdjacentHTML("afterend", '<p id="environmentDatabaseNotice" class="database-managed-note">This connection is supplied by the server environment. Change <code>DATABASE_URL</code> or its Key Vault secret instead of saving credentials here.</p>');
    }
  };

  const baseLoadUsers = loadUsers;
  loadUsers = async () => {
    await baseLoadUsers();
    userRows.querySelectorAll(".tag").forEach(tag => { tag.textContent = roleName(tag.textContent); });
  };

  const baseSyncNavigation = window.syncRootAdminNavigation;
  window.syncRootAdminNavigation = () => {
    baseSyncNavigation?.();
    const root = isRootContext();
    scopeLabel.textContent = root ? "MSP WORKSPACE" : "CUSTOMER WORKSPACE";
    company.title = root ? "MSP workspace" : companyName();
    brandIntroTitle.textContent = "Workspace identity";
    brandIntroCopy.textContent = "Set the approved MSP identity used throughout the shared operator workspace.";
  };

  const baseRefresh = refresh;
  refresh = async () => {
    await baseRefresh();
    const rootOption = company.querySelector('option[value="__root__"]');
    if (rootOption) rootOption.textContent = "MSP workspace";
    window.syncRootAdminNavigation?.();
    if (isRootContext()) { pageTitle.textContent = "MSP overview"; crumb.textContent = "MSP WORKSPACE / OVERVIEW"; }
  };

  switchCompany = async () => {
    currentCompany = company.value;
    window.syncRootAdminNavigation?.();
    showPage(isRootContext() ? "msp-overview" : "dashboard");
    await loadAll();
    if (isRootContext()) { pageTitle.textContent = "MSP overview"; crumb.textContent = "MSP WORKSPACE / OVERVIEW"; }
  };

  const baseShowPage = showPage;
  showPage = id => {
    baseShowPage(id);
    if (id === "msp-overview") {
      pageTitle.textContent = "MSP overview";
      crumb.textContent = "MSP WORKSPACE / OVERVIEW";
    }
    window.syncRootAdminNavigation?.();
  };

  accentInput.addEventListener("input", () => { accentHex.value = accentInput.value.toUpperCase(); });
  accentHex.addEventListener("input", () => {
    const value = accentHex.value.trim();
    if (/^#[0-9a-f]{6}$/i.test(value)) {
      accentInput.value = value;
      accentInput.dispatchEvent(new Event("input", {bubbles:true}));
    }
  });
  const baseBrandLoad = loadBrand;
  loadBrand = async () => { await baseBrandLoad(); accentHex.value = accentInput.value.toUpperCase(); };

  newAsset = () => {
    editingAssetId = null;
    assetFormTitle.textContent = "Add asset";
    assetSaveButton.textContent = "Save asset";
    assetName.value = ""; assetType.value = "Device"; assetStatus.value = "Active"; assetValues.value = "";
    applyAssetMetadata({operationalStatus:"healthy"}); showPage("add");
  };
})();
