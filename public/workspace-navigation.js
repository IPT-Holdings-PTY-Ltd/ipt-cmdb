/* Keeps root tools separate from selected-customer workflows. */
(() => {
  const rootRole = () => ["platform_admin", "msp_operator"].includes(me?.role);
  const groupScope = document.createElement("label");
  groupScope.id = "userGroupScope";
  groupScope.innerHTML = 'MSP access group <small>Optional; group access is combined with additional customer access below.</small><select id="userAccessGroup"><option value="">No access group</option></select>';
  document.getElementById("rootScope").before(groupScope);
  window.loadAccessGroups = async () => {
    if (!isRootContext()) return;
    const groups = await api("/api/access-groups");
    userAccessGroup.innerHTML = `<option value="">No access group</option>${groups.map(group => `<option value="${group.id}">${esc(group.name)} (${group.companyIds.length} customers)</option>`).join("")}`;
  };
  const baseToggleUserScope = toggleUserScope;
  toggleUserScope = () => {
    baseToggleUserScope();
    const showMspAccess = rootRole() && isRootContext();
    rootScope.hidden = !showMspAccess;
    groupScope.hidden = !showMspAccess;
    rootScope.style.display = showMspAccess ? "" : "none";
    groupScope.style.display = showMspAccess ? "" : "none";
  };

  window.loadCustomers = async () => {
    if (me?.role !== "platform_admin") return;
    const list = await api("/api/companies");
    customerRows.innerHTML = list.map(company => `<p><b>${esc(company.name)}</b> <span class="tag">${esc(company.id)}</span> <button class="small" type="button" onclick="openCustomerWorkspace('${company.id}')">Open customer</button></p>`).join("") || "<p>No customers yet.</p>";
  };
  window.createCustomer = async event => {
    event.preventDefault();
    try {
      await api("/api/companies", {method:"POST", body:JSON.stringify({name:customerName.value, slug:customerSlug.value})});
      event.target.reset(); await refresh(); await loadCustomers(); alert("Customer created. Select it from the scope menu to add its CIs and users.");
    } catch (error) { alert(error.message); }
  };

  const baseSyncRootNavigation = window.syncRootAdminNavigation;
  window.syncRootAdminNavigation = () => {
    baseSyncRootNavigation?.();
    const root = rootRole() && isRootContext(), platform = me?.role === "platform_admin";
    const customerOverview = document.querySelector('[data-page="dashboard"]');
    const mspOverview = document.querySelector('[data-page="msp-overview"]');
    customerOverview.hidden = root;
    mspOverview.hidden = !root;
    mspOverview.textContent = "MSP overview";
    document.querySelector('[data-page="customers"]').hidden = !(root && platform);
    ["assets", "relationships"].forEach(page => document.querySelector(`[data-page="${page}"]`).hidden = root);
    document.querySelector('[data-page="add"]').hidden = true;
    document.querySelector('[data-page="users"]').hidden = !rootRole();
    document.querySelector('[data-page="integrations"]').hidden = !root;
    document.querySelector('[data-page="branding"]').hidden = !root;
    document.querySelector('[data-page="database"]').hidden = !(root && platform);
    document.querySelector("#branding h2").textContent = root ? "MSP branding" : "Company branding";
    document.querySelector("#branding p").textContent = root ? "Brand the MSP/root workspace. Customer branding can be introduced later as a separately permissioned scope." : "Branding appears in the left menu when this customer is selected.";
    document.querySelector("#users h2").textContent = root ? "MSP user administration" : "Customer access";
    document.querySelector("#users p").textContent = root ? "Create MSP users with an optional access group and additional customer access." : "The selected customer is the security boundary. Customer users receive access only to it.";
    rootScope.firstChild.textContent = root ? "Additional customer access " : "Customer permissions ";
    if (root) {
      userAccountType.innerHTML = '<option value="root">MSP user — customer access from groups and multi-select</option>';
      userAccountType.value = "root";
    } else {
      userAccountType.innerHTML = '<option value="customer">Customer user — selected customer only</option>';
      userAccountType.value = "customer";
    }
    toggleUserScope();
    syncNavigationSections();
  };

  function syncNavigationSections() {
    const nav = document.getElementById("workspaceNav");
    if (!nav) return;
    const items = [...nav.children];
    items.forEach((item, index) => {
      if (!item.classList.contains("nav-section")) return;
      const next = items.slice(index + 1).findIndex(candidate => candidate.classList.contains("nav-section"));
      const sectionItems = items.slice(index + 1, next < 0 ? undefined : index + 1 + next);
      item.hidden = !sectionItems.some(candidate => candidate.matches("button.nav") && !candidate.hidden);
    });
  }

  const baseShowPage = showPage;
  showPage = id => {
    if (isRootContext() && id === "dashboard") id = "msp-overview";
    baseShowPage(id);
    if (id === "msp-overview") { pageTitle.textContent = "MSP overview"; crumb.textContent = "MSP / ALL CUSTOMERS"; }
    if (id === "customers") { pageTitle.textContent = "Customers"; loadCustomers(); }
    if (id === "users") { pageTitle.textContent = isRootContext() ? "MSP users" : "Customer users"; Promise.all([loadUsers(), loadAccessGroups()]); }
    if (id === "branding") { pageTitle.textContent = "MSP branding"; loadBrand(); }
  };
})();
