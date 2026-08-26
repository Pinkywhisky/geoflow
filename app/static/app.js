document.querySelectorAll("[data-file-input]").forEach((input) => {
  input.addEventListener("change", () => {
    const output = input.parentElement.querySelector("[data-file-name]");
    if (output) output.textContent = input.files[0]?.name || "Aucun fichier sélectionné";
  });
});

const filter = document.querySelector("[data-zone-filter]");
if (filter) {
  filter.addEventListener("input", () => {
    const needle = filter.value.trim().toLocaleLowerCase("fr");
    document.querySelectorAll("[data-zone-card]").forEach((card) => {
      card.hidden = !card.dataset.search.toLocaleLowerCase("fr").includes(needle);
    });
  });
}
