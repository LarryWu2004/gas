const fs = require("fs");
const path = require("path");

const htmlPath = path.join(__dirname, "..", "gas_diagnosis", "static", "index.html");
const html = fs.readFileSync(htmlPath, "utf8");
const scriptMatch = html.match(/<script>([\s\S]*)<\/script>/);

if (!scriptMatch) {
  throw new Error("frontend script block is missing");
}

new Function(scriptMatch[1]);

const elementIds = new Set(
  [...html.matchAll(/id="([^"]+)"/g)].map((match) => match[1]),
);
const referencedIds = [
  ...scriptMatch[1].matchAll(/\$\("([^"]+)"\)/g),
].map((match) => match[1]);
const loopBoundIds = [
  ...scriptMatch[1].matchAll(
    /\[((?:\s*"[^"]+"\s*,?)+)\]\.forEach\(function\(id\)\{\$\(id\)\.addEventListener/g,
  ),
].flatMap((match) => [...match[1].matchAll(/"([^"]+)"/g)].map((item) => item[1]));
referencedIds.push(...loopBoundIds);
const missingIds = [...new Set(referencedIds.filter((id) => !elementIds.has(id)))];

if (!referencedIds.includes("diagnoseBtn")) {
  throw new Error("diagnoseBtn has no click binding");
}

const sidebarScrollIndex = html.indexOf('<div class="sidebar-scroll">');
const sidebarActionIndex = html.indexOf('<div class="sidebar-action">');
const diagnoseButtonIndex = html.indexOf('id="diagnoseBtn"');
if (
  sidebarScrollIndex < 0 ||
  sidebarActionIndex < sidebarScrollIndex ||
  diagnoseButtonIndex < sidebarActionIndex ||
  !/\.sidebar-action\s*\{[\s\S]*?flex:\s*0\s+0\s+auto;/.test(html)
) {
  throw new Error("diagnose action must remain outside the scrollable sidebar content");
}

const rightRailIndex = html.indexOf('<div class="right-rail">');
const rulesPanelIndex = html.indexOf('id="rulesPanel"');
const aiPanelIndex = html.indexOf('id="aiPanel"');
if (
  rightRailIndex < 0 ||
  rulesPanelIndex < rightRailIndex ||
  aiPanelIndex < rulesPanelIndex
) {
  throw new Error("AI analysis panel must remain below the rules panel in the right rail");
}

const shellRule = html.match(/\.shell\s*\{[^}]*\}/)?.[0] || "";
if (/(?:^|[;{])\s*height:\s*calc\(100vh\s*-\s*64px\)/.test(shellRule)) {
  throw new Error("main shell must keep natural height so diagnosis content is not clipped");
}
if (
  shellRule.includes("--app-shell-min-height") ||
  html.includes('id="zoomSlider"') ||
  scriptMatch[1].includes("function setZoom") ||
  scriptMatch[1].includes("style.zoom") ||
  scriptMatch[1].includes("gas_zoom")
) {
  throw new Error("the removed page zoom feature must not be reintroduced");
}
if (!/\.main\s*\{[^}]*overflow:\s*visible;/.test(html)) {
  throw new Error("the diagnosis page must preserve natural vertical scrolling");
}

const reportActionsIndex = html.indexOf('id="linksRow"');
const reportExportIndex = html.indexOf('id="reportExport"');
const qualityViewIndex = html.indexOf('id="view-quality"');
const reportRightRailIndex = html.indexOf('<div class="right-rail">');
const reportActionsCount = [...html.matchAll(/id="linksRow"/g)].length;
if (
  reportActionsCount !== 1 ||
  reportExportIndex < 0 ||
  reportActionsIndex < 0 ||
  reportExportIndex < qualityViewIndex ||
  reportActionsIndex < reportExportIndex ||
  reportRightRailIndex < reportActionsIndex ||
  !scriptMatch[1].includes("overview_pdf") ||
  !scriptMatch[1].includes("downloadBatchPdfReports") ||
  !scriptMatch[1].includes("report-action primary")
) {
  throw new Error("PDF report actions must remain in the diagnosis detail footer");
}

if (missingIds.length) {
  throw new Error(`event bindings reference missing elements: ${missingIds.join(", ")}`);
}

const rulesLink = html.match(/<a\s+class="nav-link"\s+href="\/rules"([^>]*)>/);
if (!rulesLink || /target="_blank"/.test(rulesLink[1])) {
  throw new Error("rules navigation must stay in the current tab");
}

const stateRestoreRequirements = [
  "DIAGNOSIS_STATE_KEY",
  "function persistDiagnosisState()",
  "function restoreDiagnosisState()",
  "sessionStorage.setItem",
  "sessionStorage.getItem",
  'window.addEventListener("pagehide",persistDiagnosisState)',
  '$("rulesLink").addEventListener("click",persistDiagnosisState)',
  "restoreDiagnosisState();",
];
const missingStateRestore = stateRestoreRequirements.filter((item) => !scriptMatch[1].includes(item));
if (missingStateRestore.length) {
  throw new Error(`diagnosis state restore is incomplete: ${missingStateRestore.join(", ")}`);
}

console.log(`frontend bindings ok (${referencedIds.length} references checked)`);
