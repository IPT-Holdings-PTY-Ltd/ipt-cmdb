/* Root-only customer-group management for reusable MSP access assignments. */
(() => {
  let editingGroupId = null, groups = [];
  const selectedCompanyIds = () => [...document.querySelectorAll('#groupCompanyChecks input:checked')].map(input => input.value);
  const renderChecks = selected => { groupCompanyChecks.innerHTML = companies.map(company => `<label class="check"><input type="checkbox" value="${company.id}" ${selected.includes(company.id) ? "checked" : ""}> ${esc(company.name)}</label>`).join(""); };
  const renderGroups = () => { accessGroupRows.innerHTML = groups.map(group => `<p><b>${esc(group.name)}</b> <span class="tag">${group.companyIds.length} customers</span><small>${group.system ? "Dynamic system group" : group.companyIds.map(id => esc(companies.find(company => company.id === id)?.name || id)).join(", ")}</small>${group.system ? "" : `<button class="small" type="button" onclick="editAccessGroup('${group.id}')">Edit</button> <button class="small logout" type="button" onclick="deleteAccessGroup('${group.id}')">Delete</button>`}</p>`).join("") || "<p>No customer groups yet.</p>"; };
  window.loadAccessGroupManagement = async () => { if (!isRootContext() || me?.role !== "platform_admin") return; groups = await api("/api/access-groups"); renderChecks([]); renderGroups(); };
  window.editAccessGroup = id => { const group = groups.find(item => item.id === id); if (!group || group.system) return; editingGroupId = id; groupFormTitle.textContent = `Edit ${group.name}`; groupSaveButton.textContent = "Save group"; groupName.value = group.name; renderChecks(group.companyIds); };
  window.cancelAccessGroupEdit = () => { editingGroupId = null; groupFormTitle.textContent = "Create customer group"; groupSaveButton.textContent = "Create group"; groupName.value = ""; renderChecks([]); };
  window.saveAccessGroup = async event => { event.preventDefault(); try { const payload = {name:groupName.value, companyIds:selectedCompanyIds()}; const path = editingGroupId ? `/api/access-groups/${editingGroupId}` : "/api/access-groups"; await api(path, {method:editingGroupId ? "PUT" : "POST", body:JSON.stringify(payload)}); cancelAccessGroupEdit(); await loadAccessGroupManagement(); } catch (error) { alert(error.message); } };
  window.deleteAccessGroup = async id => { if (!confirm("Delete this customer group? Existing MSP users keep their current direct customer permissions.")) return; try { await api(`/api/access-groups/${id}`, {method:"DELETE"}); await loadAccessGroupManagement(); } catch (error) { alert(error.message); } };
  const baseSync = window.syncRootAdminNavigation;
  window.syncRootAdminNavigation = () => { baseSync?.(); document.querySelector('[data-page="access-groups"]').hidden = !(isRootContext() && me?.role === "platform_admin"); };
  const baseShowPage = showPage;
  showPage = id => { baseShowPage(id); if (id === "access-groups") { pageTitle.textContent = "Customer groups"; loadAccessGroupManagement(); } };
})();
