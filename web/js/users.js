// Users (admin) tab: list, create, remove.
"use strict";
import { $, api, SESSION } from "./api.js";
import { toastErr, toastOk, confirmDialog } from "./ui.js";

export async function loadUsers() {
  const list = $("#users-list");
  try {
    const { users } = await api("/api/users");
    list.innerHTML = "";
    for (const u of users) {
      const el = document.createElement("div");
      el.className = "card";
      el.innerHTML = `<div class="card-row">
          <h2></h2><span class="pill"></span></div>
        <div class="card-row"><span class="muted id"></span></div>`;
      $("h2", el).textContent = u.username;
      const pill = $(".pill", el);
      pill.textContent = u.role;
      if (u.role === "admin") pill.classList.add("admin");
      if (u.id !== SESSION.id) {
        const del = document.createElement("button");
        del.className = "btn btn-danger btn-sm";
        del.textContent = "Remove";
        del.onclick = () => removeUser(u.id, u.username);
        $(".card-row:last-child", el).appendChild(del);
      } else {
        $(".id", el).textContent = "you";
      }
      list.appendChild(el);
    }
  } catch (ex) {
    list.innerHTML = `<p class="muted"></p>`;
    $(".muted", list).textContent = ex.message;
  }
}

async function removeUser(id, name) {
  const ok = await confirmDialog({ title: `Remove user “${name}”?`,
    message: "Their sessions and notifications will be revoked immediately. " +
      "If they still own devices or dashboards, remove those resources and try again.",
    okLabel: "Remove", danger: true });
  if (!ok) return;
  try { await api("/api/users?id=" + encodeURIComponent(id), { method: "DELETE" }); loadUsers(); toastOk("User removed."); }
  catch (ex) { toastErr(ex.message); }
}

$("#add-user-btn").addEventListener("click", () => {
  $("#add-user-form").hidden = false;
  $("#nu-user").focus();
});
$("#nu-cancel").addEventListener("click", () => { $("#add-user-form").hidden = true; });
$("#add-user-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("#users-err"); err.hidden = true;
  try {
    await api("/api/users", { method: "POST", body: JSON.stringify({
      username: $("#nu-user").value.trim(),
      password: $("#nu-pass").value,
      role: $("#nu-role").value,
    })});
    $("#nu-user").value = ""; $("#nu-pass").value = "";
    $("#add-user-form").hidden = true;
    loadUsers();
  } catch (ex) { err.textContent = ex.message; err.hidden = false; }
});
