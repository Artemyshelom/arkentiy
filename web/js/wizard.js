const STEPS = [
  { label: "Компания", short: "1" },
  { label: "Города", short: "2" },
  { label: "Модули", short: "3" },
  { label: "iiko", short: "4" },
  { label: "Telegram", short: "5" },
  { label: "Обзор", short: "6" },
];

const PRICING = {
  basePricePerBranch: 5000,
  financePricePerBranch: 2000,
  competitorsPricePerCity: 1000,
  competitorsSetupPerCity: 3000,
  connectionFee: 10000,
  volumeDiscounts: [
    { min: 11, discount: null },
    { min: 7, discount: 0.15 },
    { min: 4, discount: 0.10 },
  ],
  annualDiscount: 0.20,
};

let currentStep = 0;
let chainData = null;
let wizardData = {
  company: "", contact: "", email: "", phone: "", password: "",
  cities: [{ name: "", branches: [{ id: "", name: "" }] }],
  modules: { finance: false, competitors: false },
  iiko: { url: "", login: "" },
  telegram: { chatId: "" },
  period: "monthly",
  promo: null,
  payMethod: "card",
  inn: "", legalName: "",
};

async function init() {
  await loadChainData();
  parseUrlParams();
  checkSavedProgress();
  renderStepper();
  showStep(currentStep);
  renderCities();
  setupPayMethodToggle();
}

async function loadChainData() {
  try {
    const resp = await fetch("/data/chain.json");
    chainData = await resp.json();
  } catch {
    chainData = { chain: "Ёбидоёби", cities: [] };
  }
}

function parseUrlParams() {
  const p = new URLSearchParams(window.location.search);
  const branchCount = parseInt(p.get("branches")) || 1;
  const cityCount = parseInt(p.get("cities")) || 1;
  const addons = (p.get("addons") || "").split(",").filter(Boolean);
  const period = p.get("period");
  const promo = p.get("promo");

  wizardData.cities = [];
  for (let i = 0; i < cityCount; i++) {
    const bc = i === 0 ? Math.max(1, branchCount - (cityCount - 1)) : 1;
    const branchArr = Array.from({ length: bc }, () => ({ id: "", name: "" }));
    wizardData.cities.push({ name: "", branches: branchArr });
  }

  if (addons.includes("finance")) wizardData.modules.finance = true;
  if (addons.includes("competitors")) wizardData.modules.competitors = true;
  if (period === "annual" || period === "monthly") wizardData.period = period;
  if (promo) wizardData.promo = { code: promo, bonuses: [], pending: true };
}

function checkSavedProgress() {
  try {
    const saved = JSON.parse(localStorage.getItem("arkentiy_register"));
    if (!saved || !saved.data) return;
    const age = Date.now() - (saved.timestamp || 0);
    if (age > 7 * 24 * 60 * 60 * 1000) { localStorage.removeItem("arkentiy_register"); return; }
    const banner = document.getElementById("resume-banner");
    const info = document.getElementById("resume-info");
    info.textContent = `${saved.data.company || "Без названия"}, шаг ${(saved.step || 0) + 1} из 6`;
    banner.classList.remove("hidden");
  } catch {}
}

function resumeRegistration() {
  try {
    const saved = JSON.parse(localStorage.getItem("arkentiy_register"));
    if (!saved) return;
    Object.assign(wizardData, saved.data);
    currentStep = saved.step || 0;
    restoreFormFields();
    renderCities();
    showStep(currentStep);
    document.getElementById("resume-banner").classList.add("hidden");
  } catch {}
}

function clearSaved() {
  localStorage.removeItem("arkentiy_register");
  document.getElementById("resume-banner").classList.add("hidden");
}

function saveProgress() {
  collectCurrentStep();
  try {
    localStorage.setItem("arkentiy_register", JSON.stringify({
      step: currentStep,
      data: wizardData,
      timestamp: Date.now(),
    }));
  } catch {}
}

function restoreFormFields() {
  const d = wizardData;
  setValue("f-company", d.company);
  setValue("f-contact", d.contact);
  setValue("f-email", d.email);
  setValue("f-phone", d.phone);
  setValue("f-iiko-url", d.iiko.url);
  setValue("f-iiko-login", d.iiko.login);
  setValue("f-chat-id", d.telegram.chatId);
  setChecked("m-finance", d.modules.finance);
  setChecked("m-competitors", d.modules.competitors);
}

function setValue(id, val) { const el = document.getElementById(id); if (el) el.value = val || ""; }
function setChecked(id, val) { const el = document.getElementById(id); if (el) el.checked = !!val; }

function renderStepper() {
  const desktop = document.getElementById("stepper-desktop");
  desktop.innerHTML = STEPS.map((s, i) => {
    const connector = i < STEPS.length - 1 ? '<div class="flex-1 h-0.5 bg-gray-200 stepper-connector" data-idx="' + i + '"></div>' : "";
    return `<div class="flex items-center gap-1.5 stepper-item cursor-pointer" data-idx="${i}" onclick="goToStep(${i})">
      <div class="w-7 h-7 rounded-full flex items-center justify-center text-xs font-semibold stepper-circle" data-idx="${i}">${i + 1}</div>
      <span class="text-xs font-medium stepper-label hidden lg:inline" data-idx="${i}">${s.label}</span>
    </div>${connector}`;
  }).join("");
  updateStepper();
}

function updateStepper() {
  document.querySelectorAll(".stepper-circle").forEach(el => {
    const idx = parseInt(el.dataset.idx);
    if (idx < currentStep) {
      el.className = "w-7 h-7 rounded-full flex items-center justify-center text-xs font-semibold bg-brand-600 text-white stepper-circle";
    } else if (idx === currentStep) {
      el.className = "w-7 h-7 rounded-full flex items-center justify-center text-xs font-semibold bg-brand-600 text-white ring-4 ring-brand-100 stepper-circle";
    } else {
      el.className = "w-7 h-7 rounded-full flex items-center justify-center text-xs font-semibold bg-gray-200 text-gray-500 stepper-circle";
    }
    el.dataset.idx = idx;
  });
  document.querySelectorAll(".stepper-connector").forEach(el => {
    const idx = parseInt(el.dataset.idx);
    el.className = idx < currentStep ? "flex-1 h-0.5 bg-brand-600 stepper-connector" : "flex-1 h-0.5 bg-gray-200 stepper-connector";
    el.dataset.idx = idx;
  });
  document.querySelectorAll(".stepper-label").forEach(el => {
    const idx = parseInt(el.dataset.idx);
    el.className = idx <= currentStep
      ? "text-xs font-medium text-brand-700 hidden lg:inline stepper-label"
      : "text-xs font-medium text-gray-400 hidden lg:inline stepper-label";
    el.dataset.idx = idx;
  });
  document.getElementById("stepper-mobile").textContent = `Шаг ${currentStep + 1} из ${STEPS.length} — ${STEPS[currentStep].label}`;
}

function showStep(idx) {
  document.querySelectorAll(".wizard-step").forEach(el => el.classList.add("hidden"));
  const step = document.querySelector(`.wizard-step[data-step="${idx}"]`);
  if (step) step.classList.remove("hidden");
  updateStepper();

  const btnBack = document.getElementById("btn-back");
  const btnNext = document.getElementById("btn-next");
  btnBack.classList.toggle("hidden", idx === 0);
  btnNext.classList.toggle("hidden", idx === STEPS.length - 1);

  if (idx === 2) { restoreModuleCheckboxes(); updateModuleSummary(); }
  if (idx === 5) buildReview();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function goToStep(idx) {
  if (idx > currentStep) return;
  collectCurrentStep();
  currentStep = idx;
  showStep(currentStep);
}

function collectCurrentStep() {
  switch (currentStep) {
    case 0:
      wizardData.company = document.getElementById("f-company").value.trim();
      wizardData.contact = document.getElementById("f-contact").value.trim();
      wizardData.email = document.getElementById("f-email").value.trim();
      wizardData.phone = document.getElementById("f-phone").value.trim();
      wizardData.password = document.getElementById("f-password").value;
      break;
    case 1:
      collectCities();
      break;
    case 2:
      wizardData.modules.finance = document.getElementById("m-finance").checked;
      wizardData.modules.competitors = document.getElementById("m-competitors").checked;
      break;
    case 3:
      wizardData.iiko.url = document.getElementById("f-iiko-url").value.trim();
      wizardData.iiko.login = document.getElementById("f-iiko-login").value.trim();
      break;
    case 4:
      wizardData.telegram.chatId = document.getElementById("f-chat-id").value.trim();
      break;
    case 5:
      wizardData.period = document.querySelector("#w-btn-annual.border-brand-600") ? "annual" : "monthly";
      const method = document.querySelector('input[name="pay-method"]:checked');
      if (method) wizardData.payMethod = method.value;
      wizardData.inn = (document.getElementById("f-inn") || {}).value || "";
      wizardData.legalName = (document.getElementById("f-legal-name") || {}).value || "";
      break;
  }
}

function validateStep(idx) {
  switch (idx) {
    case 0: {
      const c = document.getElementById("f-company").value.trim();
      const n = document.getElementById("f-contact").value.trim();
      const e = document.getElementById("f-email").value.trim();
      const p = document.getElementById("f-password").value;
      if (c.length < 2) { alert("Укажите название сети"); return false; }
      if (n.length < 2) { alert("Укажите контактное лицо"); return false; }
      if (!e || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(e)) { alert("Укажите корректный email"); return false; }
      if (p.length < 8) { alert("Пароль должен быть минимум 8 символов"); return false; }
      return true;
    }
    case 1: {
      collectCities();
      for (const city of wizardData.cities) {
        if (!city.name) { alert("Выберите город"); return false; }
        for (const b of city.branches) {
          if (!b.name) { alert(`Выберите точку в городе ${city.name}`); return false; }
        }
      }
      return true;
    }
    default: return true;
  }
}

function nextStep() {
  if (!validateStep(currentStep)) return;
  collectCurrentStep();
  saveProgress();
  if (currentStep < STEPS.length - 1) {
    currentStep++;
    showStep(currentStep);
  }
}

function prevStep() {
  collectCurrentStep();
  if (currentStep > 0) {
    currentStep--;
    showStep(currentStep);
  }
}

// --- Cities (dropdown-based) ---

function getSelectedCityNames() {
  return wizardData.cities.map(c => c.name).filter(Boolean);
}

function getSelectedBranchIds(cityIdx) {
  return wizardData.cities[cityIdx].branches.map(b => b.id).filter(Boolean);
}

function getCityBranches(cityName) {
  if (!chainData) return [];
  const city = chainData.cities.find(c => c.name === cityName);
  return city ? city.branches : [];
}

function buildCityOptions(currentValue) {
  const selected = getSelectedCityNames();
  let html = '<option value="">Выберите город</option>';
  if (!chainData) return html;
  for (const city of chainData.cities) {
    const taken = selected.includes(city.name) && city.name !== currentValue;
    if (taken) continue;
    const sel = city.name === currentValue ? " selected" : "";
    html += `<option value="${esc(city.name)}"${sel}>${esc(city.name)} (${city.branches.length})</option>`;
  }
  return html;
}

function buildBranchOptions(cityName, currentBranchId, cityIdx) {
  const branches = getCityBranches(cityName);
  const selectedIds = getSelectedBranchIds(cityIdx);
  let html = '<option value="">Выберите точку</option>';
  for (const b of branches) {
    const taken = selectedIds.includes(b.id) && b.id !== currentBranchId;
    if (taken) continue;
    const sel = b.id === currentBranchId ? " selected" : "";
    html += `<option value="${esc(b.id)}" data-name="${esc(b.name)}"${sel}>${esc(b.name)}</option>`;
  }
  return html;
}

function renderCities() {
  const container = document.getElementById("cities-container");
  container.innerHTML = wizardData.cities.map((city, ci) => {
    const cityOptions = buildCityOptions(city.name);
    const allBranches = getCityBranches(city.name);
    const selectedCount = city.branches.filter(b => b.id).length;
    const canAddBranch = city.name && selectedCount < allBranches.length;

    const branches = city.branches.map((b, bi) => {
      const branchOptions = city.name ? buildBranchOptions(city.name, b.id, ci) : '<option value="">Сначала выберите город</option>';
      return `
      <div class="flex items-center gap-2">
        <select data-city="${ci}" data-branch="${bi}" onchange="onBranchSelect(this)"
          class="flex-1 px-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 bg-white"
          ${!city.name ? "disabled" : ""}>
          ${branchOptions}
        </select>
        ${city.branches.length > 1 ? `<button onclick="removeBranch(${ci},${bi})" class="p-1 text-gray-400 hover:text-red-500"><svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg></button>` : ""}
      </div>
    `;
    }).join("");

    return `
      <div class="bg-white border border-gray-200 rounded-xl p-5">
        <div class="flex items-center justify-between mb-3">
          <label class="text-sm font-medium text-gray-700">Город ${ci + 1}</label>
          ${wizardData.cities.length > 1 ? `<button onclick="removeCity(${ci})" class="text-xs text-gray-400 hover:text-red-500">Удалить город</button>` : ""}
        </div>
        <select data-city-select="${ci}" onchange="onCitySelect(this)"
          class="w-full px-4 py-2.5 border border-gray-200 rounded-xl text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 mb-3 bg-white">
          ${cityOptions}
        </select>
        <p class="text-xs text-gray-500 mb-2">Точки в этом городе:</p>
        <div class="space-y-2 ml-2">${branches}</div>
        ${canAddBranch ? `<button onclick="addBranch(${ci})" class="mt-2 ml-2 text-xs text-brand-600 hover:text-brand-700 font-medium">+ Добавить точку</button>` : ""}
      </div>
    `;
  }).join("");

  updateAddCityButton();
  updateCitiesSummary();
}

function onCitySelect(el) {
  const ci = parseInt(el.dataset.citySelect);
  const newCity = el.value;
  wizardData.cities[ci].name = newCity;
  wizardData.cities[ci].branches = [{ id: "", name: "" }];
  renderCities();
}

function onBranchSelect(el) {
  const ci = parseInt(el.dataset.city);
  const bi = parseInt(el.dataset.branch);
  const selectedOption = el.options[el.selectedIndex];
  wizardData.cities[ci].branches[bi] = {
    id: el.value,
    name: selectedOption ? selectedOption.dataset.name || selectedOption.textContent : "",
  };
  renderCities();
}

function addCity() {
  wizardData.cities.push({ name: "", branches: [{ id: "", name: "" }] });
  renderCities();
}

function removeCity(ci) {
  wizardData.cities.splice(ci, 1);
  renderCities();
}

function addBranch(ci) {
  wizardData.cities[ci].branches.push({ id: "", name: "" });
  renderCities();
}

function removeBranch(ci, bi) {
  wizardData.cities[ci].branches.splice(bi, 1);
  renderCities();
}

function collectCities() {
  document.querySelectorAll("[data-city-select]").forEach(el => {
    const ci = parseInt(el.dataset.citySelect);
    wizardData.cities[ci].name = el.value;
  });
  document.querySelectorAll("[data-city][data-branch]").forEach(el => {
    const ci = parseInt(el.dataset.city);
    const bi = parseInt(el.dataset.branch);
    const opt = el.options[el.selectedIndex];
    wizardData.cities[ci].branches[bi] = {
      id: el.value,
      name: opt ? opt.dataset.name || opt.textContent : "",
    };
  });
}

function updateAddCityButton() {
  const btn = document.getElementById("btn-add-city");
  if (!btn || !chainData) return;
  const selected = getSelectedCityNames();
  const allUsed = chainData.cities.length > 0 && selected.length >= chainData.cities.length;
  btn.classList.toggle("hidden", allUsed);
}

function updateCitiesSummary() {
  const total = wizardData.cities.reduce((s, c) => s + c.branches.filter(b => b.id).length, 0);
  const cityCount = wizardData.cities.filter(c => c.name).length;
  document.getElementById("cities-summary").textContent = total > 0
    ? `Выбрано: ${total} ${pluralize(total, "точка", "точки", "точек")} в ${cityCount} ${pluralize(cityCount, "городе", "городах", "городах")}`
    : "";
}

function esc(s) { return (s || "").replace(/"/g, "&quot;").replace(/</g, "&lt;"); }

function pluralize(n, one, few, many) {
  const abs = Math.abs(n) % 100;
  const last = abs % 10;
  if (abs > 10 && abs < 20) return many;
  if (last > 1 && last < 5) return few;
  if (last === 1) return one;
  return many;
}

// --- Modules ---

function restoreModuleCheckboxes() {
  setChecked("m-finance", wizardData.modules.finance);
  setChecked("m-competitors", wizardData.modules.competitors);
}

function updateModuleSummary() {
  const bc = getBranchCount();
  const cc = getCityCount();
  let base = PRICING.basePricePerBranch * bc;
  let fin = document.getElementById("m-finance").checked ? PRICING.financePricePerBranch * bc : 0;
  let comp = document.getElementById("m-competitors").checked ? PRICING.competitorsPricePerCity * cc : 0;
  const total = base + fin + comp;
  let html = `<p><strong>Ваша конфигурация:</strong> ${bc} ${pluralize(bc, "точка", "точки", "точек")}, ${cc} ${pluralize(cc, "город", "города", "городов")}</p>`;
  html += `<p>Базовый: ${fmt(base)} ₽/мес</p>`;
  if (fin) html += `<p>Финансы: +${fmt(fin)} ₽/мес</p>`;
  if (comp) html += `<p>Конкуренты: +${fmt(comp)} ₽/мес</p>`;
  html += `<p class="font-semibold mt-2">Итого: ${fmt(total)} ₽/мес</p>`;
  document.getElementById("module-summary").innerHTML = html;
}

function getBranchCount() {
  return wizardData.cities.reduce((s, c) => s + c.branches.filter(b => b.id).length, 0);
}

function getCityCount() {
  return wizardData.cities.filter(c => c.name).length;
}

function fmt(n) { return n.toLocaleString("ru-RU"); }

// --- iiko Test ---

function testIiko() {
  const url = document.getElementById("f-iiko-url").value.trim();
  const login = document.getElementById("f-iiko-login").value.trim();
  if (!url || !login) { alert("Заполните URL и логин"); return; }
  const btn = document.getElementById("btn-test-iiko");
  const result = document.getElementById("iiko-result");
  btn.disabled = true;
  btn.textContent = "Проверяем...";
  result.classList.add("hidden");

  setTimeout(() => {
    btn.disabled = false;
    btn.textContent = "Проверить подключение";
    result.innerHTML = '<span class="text-amber-600">&#9888; Проверка подключения будет доступна после запуска бэкенда. Можете продолжить.</span>';
    result.classList.remove("hidden");
  }, 1500);
}

// --- Telegram Test ---

function testTelegram() {
  const chatId = document.getElementById("f-chat-id").value.trim();
  if (!chatId) { alert("Введите Chat ID"); return; }
  const btn = document.getElementById("btn-test-tg");
  const result = document.getElementById("tg-result");
  btn.disabled = true;
  btn.textContent = "Проверяем...";
  result.classList.add("hidden");

  setTimeout(() => {
    btn.disabled = false;
    btn.textContent = "Проверить подключение";
    result.innerHTML = '<span class="text-amber-600">&#9888; Проверка Telegram будет доступна после запуска бэкенда. Можете продолжить.</span>';
    result.classList.remove("hidden");
  }, 1500);
}

// --- Review (Step 6) ---

function buildReview() {
  const d = wizardData;
  const bc = getBranchCount();
  const cc = getCityCount();

  document.getElementById("review-company").innerHTML = `
    <p class="text-gray-500 text-xs uppercase tracking-wide mb-1">Компания</p>
    <p class="font-medium">${esc(d.company)} <button onclick="goToStep(0)" class="text-brand-600 text-xs ml-2">изменить</button></p>
    <p class="text-gray-500">${esc(d.contact)} &middot; ${esc(d.email)}</p>
  `;

  const cityList = d.cities
    .filter(c => c.name)
    .map(c => `${esc(c.name)}: ${c.branches.filter(b => b.name).map(b => esc(b.name)).join(", ")}`)
    .join("<br>");
  document.getElementById("review-cities").innerHTML = `
    <p class="text-gray-500 text-xs uppercase tracking-wide mb-1 mt-3">Города и точки</p>
    <p>${cityList} <button onclick="goToStep(1)" class="text-brand-600 text-xs ml-2">изменить</button></p>
  `;

  const mods = ["Базовый"];
  if (d.modules.finance) mods.push("Финансы");
  if (d.modules.competitors) mods.push("Конкуренты");
  document.getElementById("review-modules").innerHTML = `
    <p class="text-gray-500 text-xs uppercase tracking-wide mb-1 mt-3">Модули</p>
    <p>${mods.join(", ")} <button onclick="goToStep(2)" class="text-brand-600 text-xs ml-2">изменить</button></p>
  `;

  document.getElementById("review-iiko").innerHTML = d.iiko.url ? `
    <p class="text-gray-500 text-xs uppercase tracking-wide mb-1 mt-3">iiko</p>
    <p>${esc(d.iiko.url)} <button onclick="goToStep(3)" class="text-brand-600 text-xs ml-2">изменить</button></p>
  ` : `<p class="text-gray-500 text-xs mt-3">iiko: не настроен <button onclick="goToStep(3)" class="text-brand-600 text-xs ml-1">настроить</button></p>`;

  document.getElementById("review-telegram").innerHTML = d.telegram.chatId ? `
    <p class="text-gray-500 text-xs uppercase tracking-wide mb-1 mt-3">Telegram</p>
    <p>Chat ID: ${esc(d.telegram.chatId)} <button onclick="goToStep(4)" class="text-brand-600 text-xs ml-2">изменить</button></p>
  ` : `<p class="text-gray-500 text-xs mt-3">Telegram: не настроен <button onclick="goToStep(4)" class="text-brand-600 text-xs ml-1">настроить</button></p>`;

  if (d.promo && d.promo.pending && d.promo.code) {
    document.getElementById("f-promo").value = d.promo.code;
  }

  recalcReview();
}

function recalcReview() {
  const d = wizardData;
  const bc = getBranchCount();
  const cc = getCityCount();

  const baseCost = PRICING.basePricePerBranch * bc;
  const finCost = d.modules.finance ? PRICING.financePricePerBranch * bc : 0;
  const compCost = d.modules.competitors ? PRICING.competitorsPricePerCity * cc : 0;
  const compSetup = d.modules.competitors ? PRICING.competitorsSetupPerCity * cc : 0;
  let monthly = baseCost + finCost + compCost;

  let bHTML = `<div class="flex justify-between"><span class="text-gray-600">Базовый ${bc} точ.</span><span class="font-medium">${fmt(baseCost)} ₽</span></div>`;
  if (finCost) bHTML += `<div class="flex justify-between"><span class="text-gray-600">Финансы ${bc} точ.</span><span class="font-medium">${fmt(finCost)} ₽</span></div>`;
  if (compCost) bHTML += `<div class="flex justify-between"><span class="text-gray-600">Конкуренты ${cc} гор.</span><span class="font-medium">${fmt(compCost)} ₽</span></div>`;
  document.getElementById("review-breakdown").innerHTML = bHTML;

  let totalDiscount = 0;
  const volTier = PRICING.volumeDiscounts.find(t => bc >= t.min);
  if (volTier && volTier.discount) {
    totalDiscount += Math.round(monthly * volTier.discount);
  }
  if (d.period === "annual") {
    totalDiscount += Math.round((monthly - totalDiscount) * PRICING.annualDiscount);
  }

  let promoMonthly = 0;
  let promoConnection = 0;
  if (d.promo && d.promo.bonuses) {
    for (const b of d.promo.bonuses) {
      if (b.type === "fixed_discount") promoMonthly += b.amount || b.value || 0;
      if (b.type === "free_connection") promoConnection = PRICING.connectionFee;
    }
    totalDiscount += promoMonthly;
  }

  const finalMonthly = Math.max(0, monthly - totalDiscount);
  document.getElementById("review-total").textContent = fmt(finalMonthly) + " ₽/мес";

  const connFee = Math.max(0, PRICING.connectionFee - promoConnection);
  const onetimeTotal = connFee + compSetup;
  if (onetimeTotal > 0) {
    document.getElementById("review-onetime-price").textContent = fmt(onetimeTotal) + " ₽";
    document.getElementById("review-onetime").classList.remove("hidden");
  } else {
    document.getElementById("review-onetime").classList.add("hidden");
  }

  const payNow = finalMonthly + onetimeTotal;
  document.getElementById("review-pay-now").textContent = fmt(payNow) + " ₽";
  document.getElementById("review-pay-note").textContent = `Подключение${onetimeTotal > 0 ? ` ${fmt(onetimeTotal)} ₽` : ""} + первый месяц ${fmt(finalMonthly)} ₽`;
  document.getElementById("btn-pay").textContent = `Оплатить ${fmt(payNow)} ₽`;
}

// --- Period toggle ---

function setWizardPeriod(p) {
  wizardData.period = p;
  const btnM = document.getElementById("w-btn-monthly");
  const btnA = document.getElementById("w-btn-annual");
  if (p === "monthly") {
    btnM.className = "p-3 rounded-xl border-2 border-brand-600 bg-brand-50 text-brand-700 font-medium text-sm text-center";
    btnA.className = "p-3 rounded-xl border-2 border-gray-200 text-gray-600 font-medium text-sm text-center hover:border-gray-300";
  } else {
    btnA.className = "p-3 rounded-xl border-2 border-brand-600 bg-brand-50 text-brand-700 font-medium text-sm text-center";
    btnM.className = "p-3 rounded-xl border-2 border-gray-200 text-gray-600 font-medium text-sm text-center hover:border-gray-300";
  }
  recalcReview();
}

// --- Payment method toggle ---

function setupPayMethodToggle() {
  document.querySelectorAll('input[name="pay-method"]').forEach(r => {
    r.addEventListener("change", () => {
      const inv = document.getElementById("invoice-fields");
      if (r.value === "invoice" && r.checked) inv.classList.remove("hidden");
      else inv.classList.add("hidden");
    });
  });
}

// --- Promo ---

function validatePromoWizard() {
  const input = document.getElementById("f-promo");
  const result = document.getElementById("promo-result-w");
  const code = input.value.trim().toUpperCase();
  if (!code) { wizardData.promo = null; result.classList.add("hidden"); recalcReview(); return; }

  const DB = {
    "EARLY": { code: "EARLY", bonuses: [{ type: "free_connection", value: 10000 }, { type: "fixed_discount", amount: 2000 }], description: "Бесплатное подключение + скидка 2 000 ₽/мес" },
    "FRIEND": { code: "FRIEND", bonuses: [{ type: "free_connection", value: 10000 }], description: "Бесплатное подключение" },
  };

  const promo = DB[code];
  if (promo) {
    wizardData.promo = promo;
    result.innerHTML = `<span class="text-green-600">&#10003; ${promo.description}</span>`;
  } else {
    wizardData.promo = null;
    result.innerHTML = `<span class="text-red-500">Промокод не найден</span>`;
  }
  result.classList.remove("hidden");
  recalcReview();
}

// --- Submit ---

function submitPayment() {
  collectCurrentStep();
  alert("Оплата будет доступна после подключения ЮKassa. Данные сохранены — мы свяжемся с вами для подключения.");
}

function submitTrial() {
  collectCurrentStep();
  alert("Триал будет доступен после запуска бэкенда. Данные сохранены — мы свяжемся с вами для подключения.");
}

document.addEventListener("DOMContentLoaded", init);
