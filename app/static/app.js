document.querySelectorAll("[data-file-input]").forEach((input) => {
  input.addEventListener("change", () => {
    const output = input.closest("form")?.querySelector("[data-file-name]");
    if (output) output.textContent = input.files[0]?.name || "Aucun fichier sélectionné";
  });
});

const globalSaveStatus = document.querySelector("[data-save-status]");
let pendingSaves = 0;

function setSaveStatus(form, state, message) {
  const local = form.querySelector("[data-form-status]");
  const time = new Date().toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
  const text = state === "saved" ? "✓ Enregistré" : message;
  if (local) {
    local.textContent = text;
    local.dataset.state = state;
  }
  if (globalSaveStatus) {
    globalSaveStatus.textContent = state === "saved" ? `✓ Enregistré à ${time}` : message;
    globalSaveStatus.dataset.state = state;
  }
}

window.addEventListener("beforeunload", (event) => {
  if (pendingSaves > 0) event.preventDefault();
});

async function autosave(form) {
  if (!form.reportValidity()) {
    setSaveStatus(form, "error", "Information requise");
    return;
  }
  setSaveStatus(form, "saving", "Enregistrement…");
  form.querySelectorAll("[aria-invalid]").forEach((field) => field.removeAttribute("aria-invalid"));
  pendingSaves += 1;
  try {
    const response = await fetch(form.action, {
      method: form.method || "POST",
      body: new FormData(form),
      headers: { Accept: "application/json" },
    });
    const payload = await response.json();
    if (!response.ok || payload.status !== "ok") {
      throw new Error(payload.message || "Échec de l’enregistrement");
    }
    setSaveStatus(form, "saved", payload.message);
    if (form.matches("[data-annex-form]")) updateAnnexState(form);
    if (form.matches("[data-reference-form]") && payload.reference) {
      document.querySelectorAll("[data-dossier-reference]").forEach((node) => {
        node.textContent = payload.reference;
      });
      document.title = document.title.replace(/—.*?—/, `— ${payload.reference} —`);
    }
  } catch (error) {
    setSaveStatus(form, "error", error.message || "Échec de l’enregistrement");
    form.querySelectorAll("input:not([type=hidden])").forEach((field) => field.setAttribute("aria-invalid", "true"));
  } finally {
    pendingSaves -= 1;
  }
}

function updateUnitForm(form) {
  const select = form.querySelector("select[name=unit]");
  const wrapper = form.querySelector("[data-unit-justification]");
  const justification = form.querySelector("input[name=justification]");
  const differs = select.value !== form.dataset.detectedUnit;
  wrapper.hidden = !differs;
  justification.required = differs;
  if (!differs) justification.value = "";
}

document.querySelectorAll("form[data-autosave]").forEach((form) => {
  if (form.matches("[data-unit-form]")) updateUnitForm(form);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    autosave(form);
  });
  form.querySelectorAll("select, textarea, input:not([type=hidden])").forEach((field) => {
    field.addEventListener("change", () => {
      if (form.matches("[data-unit-form]")) updateUnitForm(form);
      autosave(form);
    });
  });
});

const layerFilter = document.querySelector("[data-layer-filter]");
if (layerFilter) {
  layerFilter.addEventListener("input", () => {
    const needle = layerFilter.value.trim().toLocaleLowerCase("fr");
    document.querySelectorAll("[data-layer-row]").forEach((row) => {
      row.hidden = !row.dataset.search.toLocaleLowerCase("fr").includes(needle);
    });
  });
}

document.querySelectorAll("form[data-confirm]").forEach((form) => {
  form.addEventListener("submit", (event) => {
    if (!window.confirm(form.dataset.confirm)) event.preventDefault();
  });
});

document.querySelectorAll("[data-delete-button]").forEach((button) => {
  button.addEventListener("click", () => {
    const form = button.closest("[data-delete-form]");
    const reference = form.dataset.reference;
    const confirmed = window.confirm(
      `Supprimer définitivement le dossier « ${reference} » ?\n\nLe dossier JSON et tous ses documents générés seront supprimés.`
    );
    if (confirmed) form.requestSubmit();
  });
});

document.querySelectorAll("form[data-busy-form]").forEach((form) => {
  form.addEventListener("submit", (event) => {
    if (event.defaultPrevented) return;
    const button = form.querySelector("button[type=submit][data-busy-label]");
    if (!button) return;
    // Defer so the button value is still part of the submitted form data.
    requestAnimationFrame(() => {
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      button.textContent = button.dataset.busyLabel;
    });
  });
});

document.querySelectorAll("form[data-auto-filter]").forEach((form) => {
  form.querySelectorAll("select").forEach((select) => {
    select.addEventListener("change", () => form.requestSubmit());
  });
});

const THEMES = ["auto", "dark", "light"];
const THEME_LABELS = { auto: "automatique", dark: "sombre", light: "clair" };

function storedTheme() {
  try {
    const value = localStorage.getItem("geoflow-theme");
    return THEMES.includes(value) ? value : "auto";
  } catch (error) {
    return "auto";
  }
}

function applyTheme(theme) {
  if (theme === "auto") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
  document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
    const label = `Thème : ${THEME_LABELS[theme]}`;
    button.dataset.current = theme;
    button.setAttribute("aria-label", label);
    button.title = label;
  });
}

let currentTheme = storedTheme();

document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
  button.addEventListener("click", () => {
    const next = THEMES[(THEMES.indexOf(currentTheme) + 1) % THEMES.length];
    currentTheme = next;
    try {
      if (next === "auto") localStorage.removeItem("geoflow-theme");
      else localStorage.setItem("geoflow-theme", next);
    } catch (error) {}
    applyTheme(next);
  });
});
applyTheme(currentTheme);

document.querySelectorAll("[data-drop-form]").forEach((form) => {
  const zone = form.querySelector("[data-dropzone]");
  const input = form.querySelector("input[type=file]");
  const title = form.querySelector("[data-drop-title]");
  const sub = form.querySelector("[data-file-name]");
  const initial = { title: title.innerHTML, sub: sub.innerHTML };
  form.classList.add("is-enhanced");

  // Back/forward cache restores the page as it was when leaving: reset it.
  window.addEventListener("pageshow", (event) => {
    if (!event.persisted) return;
    zone.classList.remove("is-busy", "is-invalid");
    title.innerHTML = initial.title;
    sub.innerHTML = initial.sub;
    input.value = "";
    form.querySelectorAll("button[aria-busy]").forEach((button) => {
      button.disabled = false;
      button.removeAttribute("aria-busy");
      button.textContent = "Obtenir le diagnostic";
    });
  });

  const submitFile = () => {
    const file = input.files[0];
    if (!file) return;
    if (!/\.(dxf|dwg)$/i.test(file.name)) {
      zone.classList.add("is-invalid");
      title.textContent = "Format non pris en charge";
      sub.textContent = `${file.name} n’est pas un fichier .dxf ou .dwg.`;
      input.value = "";
      return;
    }
    zone.classList.remove("is-invalid");
    zone.classList.add("is-busy");
    title.textContent = `Analyse de ${file.name}…`;
    sub.textContent = "Lecture du plan, détection des lots et contrôles en cours.";
    form.requestSubmit();
  };

  input.addEventListener("change", submitFile);
  ["dragenter", "dragover"].forEach((type) => {
    zone.addEventListener(type, (event) => {
      event.preventDefault();
      zone.classList.add("is-dragover");
    });
  });
  ["dragleave", "drop"].forEach((type) => {
    zone.addEventListener(type, () => zone.classList.remove("is-dragover"));
  });
  zone.addEventListener("drop", (event) => {
    event.preventDefault();
    if (!event.dataTransfer?.files.length) return;
    input.files = event.dataTransfer.files;
    submitFile();
  });
  // A file dropped next to the zone must not make the browser leave the page.
  window.addEventListener("dragover", (event) => event.preventDefault());
  window.addEventListener("drop", (event) => event.preventDefault());
});

const dossierSearch = document.querySelector("[data-dossier-search]");
if (dossierSearch) {
  const rows = document.querySelectorAll("[data-dossier-list] > li");
  const empty = document.querySelector("[data-dossier-empty]");
  dossierSearch.addEventListener("input", () => {
    const needle = dossierSearch.value.trim().toLocaleLowerCase("fr");
    let visible = 0;
    rows.forEach((row) => {
      const match = row.dataset.search.toLocaleLowerCase("fr").includes(needle);
      row.hidden = !match;
      if (match) visible += 1;
    });
    if (empty) empty.hidden = visible > 0;
  });
}

function updateAnnexState(form) {
  const item = form.closest("[data-annex-state]");
  const select = form.querySelector("select[name=lot_id]");
  if (!item || !select) return;
  const label = item.querySelector("[data-annex-label]");
  const attached = Boolean(select.value);
  item.dataset.annexState = attached ? "rattachee" : form.dataset.proposedLot ? "proposee" : "non_rattachee";
  item.className = item.className.replace(/state-\S+/, `state-${item.dataset.annexState}`);
  if (label && attached) {
    label.textContent = `Rattachée : ${select.selectedOptions[0].textContent}`;
    label.className = "status-pill ok";
  } else if (label) {
    label.textContent = "Non rattachée";
    label.className = "status-pill neutral";
  }
}

document.querySelectorAll("[data-annex-accept]").forEach((button) => {
  button.addEventListener("click", () => {
    const form = button.closest("[data-annex-form]");
    const select = form.querySelector("select[name=lot_id]");
    select.value = form.dataset.proposedLot;
    select.dispatchEvent(new Event("change"));
  });
});

const annexFilter = document.querySelector("[data-annex-filter]");
if (annexFilter) {
  const items = document.querySelectorAll("[data-annex-list] > li");
  const empty = document.querySelector("[data-annex-empty]");
  const applyAnnexFilter = () => {
    const wanted = annexFilter.value;
    let visible = 0;
    items.forEach((item) => {
      const state = item.dataset.annexState;
      const match =
        wanted === "all" || (wanted === "todo" ? state !== "rattachee" : state === wanted);
      item.hidden = !match;
      if (match) visible += 1;
    });
    if (empty) empty.hidden = visible > 0;
  };
  annexFilter.addEventListener("change", applyAnnexFilter);
  applyAnnexFilter();
}

// Drawing profile wizard ----------------------------------------------------

const profileForm = document.querySelector("[data-profile-form]");
if (profileForm) {
  const steps = [...profileForm.querySelectorAll("[data-step]")];
  const tabs = [...document.querySelectorAll("[data-step-tab]")];
  const prev = profileForm.querySelector("[data-step-prev]");
  const next = profileForm.querySelector("[data-step-next]");
  let current = Number((location.hash.match(/etape-(\d)/) || [])[1] || 1);

  const showStep = (number, focus = true) => {
    current = Math.min(Math.max(number, 1), steps.length);
    steps.forEach((step) => step.classList.toggle("is-active", Number(step.dataset.step) === current));
    tabs.forEach((tab) => {
      const active = Number(tab.dataset.stepTab) === current;
      tab.classList.toggle("current", active);
      if (active) {
        tab.setAttribute("aria-current", "step");
        tab.scrollIntoView({ block: "nearest", inline: "nearest" });
      }
      else tab.removeAttribute("aria-current");
    });
    prev.hidden = current === 1;
    next.hidden = current === steps.length;
    history.replaceState(null, "", `#etape-${current}`);
    if (focus) {
      const heading = steps[current - 1].querySelector("h2");
      heading.setAttribute("tabindex", "-1");
      heading.focus({ preventScroll: true });
      window.scrollTo({ top: 0, behavior: "smooth" });
    }
  };
  prev.addEventListener("click", () => showStep(current - 1));
  next.addEventListener("click", () => showStep(current + 1));
  tabs.forEach((tab) => tab.addEventListener("click", () => showStep(Number(tab.dataset.stepTab))));
  showStep(current, false);

  // Live check: debounced, cancels the previous request, never saves.
  const previewUrl = profileForm.dataset.previewUrl;
  const errorBox = document.querySelector("[data-profile-errors]");
  const statusPill = document.querySelector("[data-profile-status]");
  const saveButton = profileForm.querySelector("[data-save-profile]");
  let timer = null;
  let controller = null;
  const refresh = () => {
    if (!previewUrl) return;
    clearTimeout(timer);
    timer = setTimeout(async () => {
      controller?.abort();
      controller = new AbortController();
      try {
        const response = await fetch(previewUrl, {
          method: "POST",
          body: new FormData(profileForm),
          headers: { Accept: "application/json" },
          signal: controller.signal,
        });
        const payload = await response.json();
        if (!payload.ok) {
          errorBox.innerHTML = payload.messages.map((item) => `<p></p>`).join("");
          errorBox.querySelectorAll("p").forEach((node, index) => (node.textContent = payload.messages[index]));
          errorBox.hidden = false;
          saveButton.disabled = true;
          return;
        }
        errorBox.hidden = true;
        saveButton.disabled = false;
        Object.entries(payload.fragments).forEach(([key, html]) => {
          profileForm.querySelectorAll(`[data-live="${key}"]`).forEach((node) => (node.innerHTML = html));
        });
        if (statusPill) {
          statusPill.textContent = payload.status_label;
          statusPill.className = `status-pill ${{ complet: "ok", avertissements: "warning" }[payload.status] || "blocked"}`;
        }
      } catch (error) {
        if (error.name !== "AbortError") {
          errorBox.textContent = "Vérification en direct indisponible ; le profil reste enregistrable.";
          errorBox.hidden = false;
        }
      }
    }, 450);
  };
  profileForm.addEventListener("input", (event) => {
    if (!event.target.matches("[data-chip-input]")) refresh();
  });
  profileForm.addEventListener("change", (event) => {
    if (!event.target.matches("[data-chip-input], [data-source-add]")) refresh();
  });
  const title = document.querySelector("[data-profile-title]");
  profileForm.querySelector("[data-profile-name]")?.addEventListener("input", (event) => {
    if (title && event.target.value.trim()) title.textContent = event.target.value.trim();
  });

  // Multi-value layer fields: chips with hidden inputs, datalist suggestions.
  const addChip = (field, value) => {
    const clean = value.trim().slice(0, 128);
    if (!clean) return false;
    const list = field.querySelector("[data-chips]");
    const exists = [...list.querySelectorAll("input")].some((input) => input.value.toLocaleLowerCase("fr") === clean.toLocaleLowerCase("fr"));
    if (exists) return false;
    const item = document.createElement("li");
    item.className = "chip";
    const label = document.createElement("span");
    label.textContent = clean;
    const hidden = document.createElement("input");
    hidden.type = "hidden";
    hidden.name = field.dataset.name;
    hidden.value = clean;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "chip-remove";
    remove.dataset.chipRemove = "";
    remove.setAttribute("aria-label", `Retirer ${clean}`);
    remove.textContent = "×";
    item.append(label, hidden, remove);
    list.append(item);
    refresh();
    return true;
  };
  profileForm.querySelectorAll("[data-chip-field]").forEach((field) => {
    const input = field.querySelector("[data-chip-input]");
    const commit = () => {
      if (addChip(field, input.value)) input.value = "";
    };
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        commit();
      } else if (event.key === "Backspace" && !input.value) {
        field.querySelector("[data-chips] li:last-child")?.remove();
        refresh();
      }
    });
    input.addEventListener("change", commit);
    field.addEventListener("click", (event) => {
      const button = event.target.closest("[data-chip-remove]");
      if (!button) return;
      button.closest("li").remove();
      input.focus();
      refresh();
    });
  });

  // Ordered sources: the order of the hidden inputs is the priority.
  profileForm.querySelectorAll("[data-source-list]").forEach((list) => {
    const renumber = () => {
      [...list.children].forEach((item, index) => {
        item.querySelector(".source-rank").textContent = index + 1;
      });
      refresh();
    };
    list.addEventListener("click", (event) => {
      const item = event.target.closest("li");
      if (!item) return;
      if (event.target.closest("[data-source-up]") && item.previousElementSibling) {
        list.insertBefore(item, item.previousElementSibling);
      } else if (event.target.closest("[data-source-down]") && item.nextElementSibling) {
        list.insertBefore(item.nextElementSibling, item);
      } else if (event.target.closest("[data-source-remove]") && list.children.length > 1) {
        item.remove();
      } else {
        return;
      }
      renumber();
    });
    const adder = list.nextElementSibling.querySelector("[data-source-add]");
    adder.addEventListener("change", () => {
      const value = adder.value;
      adder.value = "";
      if (!value || list.querySelector(`li[data-value="${value}"]`)) return;
      const template = list.firstElementChild.cloneNode(true);
      template.dataset.value = value;
      template.querySelector("input").value = value;
      template.querySelector(".source-label").textContent = adder.querySelector(`option[value="${value}"]`).textContent;
      list.append(template);
      renumber();
    });
  });

  // Level aliases: observed value → business value rows.
  const aliasRows = profileForm.querySelector("[data-alias-rows]");
  profileForm.querySelector("[data-alias-add]")?.addEventListener("click", () => {
    const row = profileForm.querySelector("[data-alias-template]").content.firstElementChild.cloneNode(true);
    aliasRows.append(row);
    row.querySelector("input").focus();
  });
  aliasRows?.addEventListener("click", (event) => {
    if (!event.target.closest("[data-alias-remove]")) return;
    event.target.closest("tr").remove();
    refresh();
  });

  // New-layer suggestions are applied only on an explicit click.
  document.querySelectorAll("[data-suggestion]").forEach((card) => {
    card.querySelector("[data-suggestion-add]").addEventListener("click", () => {
      const field = profileForm.querySelector(`[data-chip-field][data-name="${card.dataset.role}"]`);
      if (field) addChip(field, card.dataset.layer);
      card.remove();
    });
    card.querySelector("[data-suggestion-ignore]").addEventListener("click", () => card.remove());
  });
}
