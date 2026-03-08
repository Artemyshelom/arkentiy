const PRICING = {
  basePricePerBranch: 5000,
  financePricePerBranch: 2000,
  competitorsPricePerCity: 1000,
  competitorsSetupPerCity: 3000,
  connectionFee: 10000,
  volumeDiscounts: [
    { min: 11, label: "10+ точек — обсудим", discount: null },
    { min: 7,  label: "7+ точек — скидка 15%", discount: 0.15 },
    { min: 4,  label: "4+ точек — скидка 10%", discount: 0.10 },
  ],
  annualDiscount: 0.20,
};

let state = {
  branches: 2,
  cities: 1,
  period: "monthly",
  addonFinance: false,
  addonCompetitors: false,
  promo: null,
};

function adjustValue(id, delta) {
  const input = document.getElementById(id);
  const val = Math.max(1, Math.min(50, parseInt(input.value || 1) + delta));
  input.value = val;
  if (id === "branches") {
    const citiesInput = document.getElementById("cities");
    if (parseInt(citiesInput.value) > val) {
      citiesInput.value = val;
    }
  }
  if (id === "cities") {
    const branchesInput = document.getElementById("branches");
    if (parseInt(branchesInput.value) < val) {
      branchesInput.value = val;
    }
  }
  calculate();
}

function setPeriod(p) {
  state.period = p;
  const btnM = document.getElementById("btn-monthly");
  const btnA = document.getElementById("btn-annual");
  if (p === "monthly") {
    btnM.className = "p-3 rounded-xl border-2 border-brand-600 bg-brand-50 text-brand-700 font-medium text-sm text-center transition-all";
    btnA.className = "p-3 rounded-xl border-2 border-gray-200 text-gray-600 font-medium text-sm text-center hover:border-gray-300 transition-all";
  } else {
    btnA.className = "p-3 rounded-xl border-2 border-brand-600 bg-brand-50 text-brand-700 font-medium text-sm text-center transition-all";
    btnM.className = "p-3 rounded-xl border-2 border-gray-200 text-gray-600 font-medium text-sm text-center hover:border-gray-300 transition-all";
  }
  calculate();
}

function fmt(n) {
  return n.toLocaleString("ru-RU");
}

function getVolumeDiscount(branches) {
  for (const tier of PRICING.volumeDiscounts) {
    if (branches >= tier.min) {
      return tier;
    }
  }
  return null;
}

function calculate() {
  const branches = Math.max(1, parseInt(document.getElementById("branches").value) || 1);
  const cities = Math.max(1, Math.min(branches, parseInt(document.getElementById("cities").value) || 1));
  state.branches = branches;
  state.cities = cities;
  state.addonFinance = document.getElementById("addon-finance").checked;
  state.addonCompetitors = document.getElementById("addon-competitors").checked;

  document.getElementById("cities").value = cities;

  const volumeTier = getVolumeDiscount(branches);
  const badge = document.getElementById("volume-badge");
  const individualBlock = document.getElementById("individual-offer");

  if (volumeTier && volumeTier.discount === null) {
    badge.textContent = volumeTier.label;
    badge.className = "ml-2 text-xs font-medium text-brand-700 bg-brand-50 px-2.5 py-1 rounded-full";
    individualBlock.classList.remove("hidden");
  } else if (volumeTier) {
    badge.textContent = volumeTier.label;
    badge.className = "ml-2 text-xs font-medium text-green-700 bg-green-50 px-2.5 py-1 rounded-full";
    individualBlock.classList.add("hidden");
  } else {
    badge.className = "hidden";
    individualBlock.classList.add("hidden");
  }

  const baseCost = PRICING.basePricePerBranch * branches;
  const financeCost = state.addonFinance ? PRICING.financePricePerBranch * branches : 0;
  const competitorsCost = state.addonCompetitors ? PRICING.competitorsPricePerCity * cities : 0;
  const competitorsSetup = state.addonCompetitors ? PRICING.competitorsSetupPerCity * cities : 0;

  let monthlySubtotal = baseCost + financeCost + competitorsCost;

  let breakdownHTML = `
    <div class="flex justify-between">
      <span class="text-gray-600">Базовый &times; ${branches} точ.</span>
      <span class="font-medium text-gray-900">${fmt(baseCost)} ₽</span>
    </div>
  `;

  if (state.addonFinance) {
    breakdownHTML += `
      <div class="flex justify-between">
        <span class="text-gray-600">Финансы &times; ${branches} точ.</span>
        <span class="font-medium text-gray-900">${fmt(financeCost)} ₽</span>
      </div>
    `;
  }

  if (state.addonCompetitors) {
    breakdownHTML += `
      <div class="flex justify-between">
        <span class="text-gray-600">Конкуренты &times; ${cities} гор.</span>
        <span class="font-medium text-gray-900">${fmt(competitorsCost)} ₽</span>
      </div>
    `;
  }

  document.getElementById("breakdown").innerHTML = breakdownHTML;

  let discountsHTML = "";
  let totalDiscount = 0;

  if (volumeTier && volumeTier.discount) {
    const volAmt = Math.round(monthlySubtotal * volumeTier.discount);
    totalDiscount += volAmt;
    discountsHTML += `
      <div class="flex justify-between text-green-600">
        <span>Скидка за объём (${Math.round(volumeTier.discount * 100)}%)</span>
        <span>&minus;${fmt(volAmt)} ₽</span>
      </div>
    `;
  }

  if (state.period === "annual") {
    const annAmt = Math.round((monthlySubtotal - totalDiscount) * PRICING.annualDiscount);
    totalDiscount += annAmt;
    discountsHTML += `
      <div class="flex justify-between text-green-600">
        <span>Годовая оплата (&minus;20%)</span>
        <span>&minus;${fmt(annAmt)} ₽</span>
      </div>
    `;
  }

  let promoMonthlyDiscount = 0;
  let promoConnectionDiscount = 0;
  if (state.promo) {
    for (const bonus of state.promo.bonuses) {
      if (bonus.type === "fixed_discount") {
        promoMonthlyDiscount += bonus.value;
      }
      if (bonus.type === "free_connection") {
        promoConnectionDiscount = PRICING.connectionFee;
      }
    }
    if (promoMonthlyDiscount > 0) {
      totalDiscount += promoMonthlyDiscount;
      discountsHTML += `
        <div class="flex justify-between text-green-600">
          <span>Промокод «${state.promo.code}»</span>
          <span>&minus;${fmt(promoMonthlyDiscount)} ₽</span>
        </div>
      `;
    }
  }

  document.getElementById("discounts-section").innerHTML = discountsHTML;

  const finalMonthly = Math.max(0, monthlySubtotal - totalDiscount);
  document.getElementById("total-price").textContent = fmt(finalMonthly);

  let oneTimeCost = PRICING.connectionFee + competitorsSetup - promoConnectionDiscount;
  oneTimeCost = Math.max(0, oneTimeCost);

  const onetimeRow = document.getElementById("onetime-row");
  if (oneTimeCost > 0) {
    let parts = [];
    const connFee = PRICING.connectionFee - promoConnectionDiscount;
    if (connFee > 0) parts.push(`подкл. ${fmt(connFee)} ₽`);
    if (competitorsSetup > 0) parts.push(`конкуренты ${fmt(competitorsSetup)} ₽`);
    document.getElementById("onetime-price").textContent = fmt(oneTimeCost) + " ₽";
    onetimeRow.classList.remove("hidden");
  } else if (competitorsSetup > 0) {
    document.getElementById("onetime-price").textContent = fmt(competitorsSetup) + " ₽";
    onetimeRow.classList.remove("hidden");
  } else {
    onetimeRow.classList.add("hidden");
  }
  updateCTALinks();
}

function validatePromo() {
  const input = document.getElementById("promo-input");
  const result = document.getElementById("promo-result");
  const code = input.value.trim().toUpperCase();

  if (!code) {
    result.classList.add("hidden");
    state.promo = null;
    calculate();
    return;
  }

  result.innerHTML = '<span class="text-gray-400">Проверяю...</span>';
  result.classList.remove("hidden");

  fetch("/api/promo/validate", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({code}),
  })
    .then(r => r.json())
    .then(data => {
      if (data.valid) {
        state.promo = {code: data.code, bonuses: data.bonuses || []};
        const desc = (data.bonuses || []).map(b => {
          if (b.type === "free_connection") return "бесплатное подключение";
          if (b.type === "fixed_discount") return `скидка ${Number(b.value).toLocaleString("ru-RU")} ₽/мес`;
          return b.type;
        }).join(" + ");
        result.innerHTML = `<span class="text-green-600">&#10003; ${escapeHtml(desc || data.description || code)}</span>`;
      } else {
        state.promo = null;
        result.innerHTML = `<span class="text-red-500">${escapeHtml(data.message || "Промокод не найден")}</span>`;
      }
      calculate();
    })
    .catch(() => {
      state.promo = null;
      result.innerHTML = '<span class="text-red-500">Ошибка проверки промокода</span>';
      calculate();
    });
}

function buildRegisterUrl(trial) {
  const addons = [];
  if (state.addonFinance) addons.push("finance");
  if (state.addonCompetitors) addons.push("competitors");
  const params = new URLSearchParams();
  params.set("branches", state.branches);
  params.set("cities", state.cities);
  if (addons.length) params.set("addons", addons.join(","));
  params.set("period", state.period);
  if (state.promo) params.set("promo", state.promo.code);
  if (trial) params.set("trial", "true");
  return "/register.html?" + params.toString();
}

function updateCTALinks() {
  const cta = document.getElementById("cta-connect");
  const trial = document.getElementById("cta-trial");
  if (cta) cta.href = buildRegisterUrl(false);
  if (trial) trial.href = buildRegisterUrl(true);
}

document.addEventListener("DOMContentLoaded", () => { calculate(); updateCTALinks(); });

function escapeHtml(s) {
  return String(s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
