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

if (missingIds.length) {
  throw new Error(`event bindings reference missing elements: ${missingIds.join(", ")}`);
}

console.log(`frontend bindings ok (${referencedIds.length} references checked)`);
