// Mirrors providers/catalog.py; no provider identities or capabilities are inferred.
const features = ['tools', 'structured', 'temperature', 'max_output_tokens', 'cache_policy', 'reasoning_effort'];
const efforts = ['off', 'low', 'medium', 'high'];
const bytes = x => JSON.stringify(x).replace(/[\u007f-\uffff]/g, character => '\\u' + character.charCodeAt(0).toString(16).padStart(4,'0')).length;
export function normalizeCatalog(input) {
  let rows, defaultSelection = null, reasoningDefault = null;
  if (!Array.isArray(input) && input && typeof input === 'object') {
    if (Object.keys(input).sort().join(',') !== 'default,models,reasoning_default' || (input.default !== null && typeof input.default !== 'string')) throw Error('Invalid provider catalog');
    rows = input.models; defaultSelection = input.default; reasoningDefault = input.reasoning_default;
    if (reasoningDefault !== null && !efforts.includes(reasoningDefault)) throw Error('Invalid provider catalog');
  }
  const fail = () => { throw Error('Invalid provider catalog'); };
  if (!Array.isArray(rows) || rows.length > 256 || bytes(rows) > 131072) fail();
  const seen = new Set();
  const catalog = rows.map(row => {
    const required = ['value','description','features','label','version','max_output_tokens','context_length','family'];
    if (!row || typeof row !== 'object' || required.some(k => !(k in row)) || Object.keys(row).some(k => ![...required,'reasoning_efforts'].includes(k))) fail();
    for (const [key, limit] of [['value',200],['description',512],['label',200],['version',100],['family',200]]) {
      const v = row[key];
      if ((key === 'version' || key === 'family') && v === null) continue;
      if (typeof v !== 'string' || [...v].length > limit || /[\p{C}\p{Z}]/u.test(v.replaceAll(' ',''))) fail();
    }
    if (!row.value || row.family === '' || seen.has(row.value)) fail();
    seen.add(row.value);
    for (const key of ['max_output_tokens','context_length']) if (row[key] !== null && (!Number.isInteger(row[key]) || row[key] < 1 || row[key] > 2147483647)) fail();
    if (!Array.isArray(row.features) || new Set(row.features).size !== row.features.length || row.features.some(x => !features.includes(x))) fail();
    if (row.reasoning_efforts !== undefined && row.reasoning_efforts !== null && (!Array.isArray(row.reasoning_efforts) || new Set(row.reasoning_efforts).size !== row.reasoning_efforts.length || row.reasoning_efforts.some(x => !efforts.includes(x)))) fail();
    const {value, label, ...metadata} = structuredClone(row);
    return {...metadata, selected_llm: value, display_name: label};
  });
  if (defaultSelection !== null && !catalog.some(row => row.selected_llm === defaultSelection)) fail();
  if (catalog.length && defaultSelection === null) fail();
  Object.defineProperty(catalog, 'defaultSelection', {value: defaultSelection});
  Object.defineProperty(catalog, 'reasoningDefault', {value: reasoningDefault});
  return catalog;
}
export function resolveSelection(catalog, {model, version = '', reasoning = ''}) {
  const selection = catalog.find(row => row.selected_llm === (model || catalog.defaultSelection));
  const effort = reasoning || catalog.reasoningDefault;
  let error = '';
  if (!selection) error = 'Choose an available model.';
  else if (version && selection.version !== version) error = 'This model version is no longer available.';
  else if (effort && (!selection.features.includes('reasoning_effort') || !selection.reasoning_efforts?.includes(effort))) {
    if (selection.reasoning_efforts === undefined || selection.reasoning_efforts === null) error = 'Reasoning support for this model is unknown; the selected or configured effort cannot be verified.';
    else error = reasoning ? 'This reasoning setting is unsupported.' : 'The configured reasoning default is unsupported for this model.';
  }
  return {valid: !error, error, selection};
}
