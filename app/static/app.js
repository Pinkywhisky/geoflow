document.querySelectorAll("[data-file-input]").forEach((input) => {
  input.addEventListener("change", () => {
    const output = input.closest("form")?.querySelector("[data-file-name]");
    if (output) output.textContent = input.files[0]?.name || "Aucun fichier sélectionné";
  });
});

const globalSaveStatus = document.querySelector("[data-save-status]");

function setSaveStatus(form, state, message) {
  const local = form.querySelector("[data-form-status]");
  const text = state === "saved" ? "✓ Enregistré" : message;
  [local, globalSaveStatus].filter(Boolean).forEach((output) => {
    output.textContent = text;
    output.dataset.state = state;
  });
}

async function autosave(form) {
  if (!form.reportValidity()) {
    setSaveStatus(form, "error", "Information requise");
    return;
  }
  setSaveStatus(form, "saving", "Enregistrement…");
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
  } catch (error) {
    setSaveStatus(form, "error", error.message || "Échec de l’enregistrement");
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
  form.querySelectorAll("select, input:not([type=hidden])").forEach((field) => {
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

document.querySelectorAll("[data-zone-form]").forEach((form) => {
  const surface = form.querySelector("input[name=retained_surface]");
  const wrapper = form.querySelector("[data-surface-justification]");
  const justification = form.querySelector("input[name=justification]");
  const geometric = Number.parseFloat(form.dataset.geometricSurface);
  const update = () => {
    const retained = Number.parseFloat(surface.value.replace(",", "."));
    const differs = Number.isFinite(retained) && Math.abs(retained - geometric) > 1e-9;
    wrapper.hidden = !differs;
    justification.required = differs;
    if (!differs) justification.value = "";
  };
  surface.addEventListener("input", update);
  update();
});

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
