/**
 * 指数估值数据 Webhook（绑定到指定 Spreadsheet 部署为 Web App）。
 *
 * 入参 JSON：
 *   {
 *     "sheetName": "指数",
 *     "headers":   ["日期","代码","名称","PE","PB", ...],
 *     "keyCols":   ["日期","代码"],
 *     "rows":      [["2026/4/27","SH000300","沪深300", ...], ...]
 *   }
 *
 * 写入策略（严格只动指定 tab，不会创建/碰其他 tab）：
 *   - 目标 tab 不存在 → 直接报错（防止误创建）
 *   - tab 为空 → 写表头 + 全部行
 *   - tab 已有数据：按 keyCols 组合查找已有行
 *       命中 → 仅用"非空"传入值覆盖对应单元格（空值不会清掉旧数据）
 *       未命中 → 追加到末尾
 *   - 表头如有新增列，追加到表头末尾（保留旧列顺序）
 */

function doPost(e) {
  try {
    const data = JSON.parse(e.postData.contents);
    const sheetName = data.sheetName || "指数";
    const headers = data.headers || [];
    const keyCols = data.keyCols || [];
    const rows = data.rows || [];

    if (headers.length === 0) {
      return _resp({status: "error", message: "missing headers"});
    }

    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const sheet = ss.getSheetByName(sheetName);
    if (!sheet) {
      // 严格模式：不存在直接报错，绝不创建新 tab，避免误动其他 sheet
      return _resp({status: "error", message: "tab 「" + sheetName + "」 不存在；请先在 Sheet 中创建该 tab，或检查 SHEET_NAME 是否拼写正确"});
    }

    // 读取现有数据
    const lastRow = sheet.getLastRow();
    const lastCol = sheet.getLastColumn();

    // 空 tab：当作初始化
    if (lastRow === 0) {
      sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
      if (rows.length > 0) {
        sheet.getRange(2, 1, rows.length, headers.length).setValues(rows);
      }
      return _resp({status: "ok", mode: "init", appended: rows.length, sheet: sheetName});
    }

    let existing = sheet.getRange(1, 1, lastRow, Math.max(lastCol, 1)).getValues();
    let existingHeader = existing[0].slice();

    // 表头扩列：传入有但 sheet 没有的列追加到末尾
    let headerChanged = false;
    headers.forEach(h => {
      if (existingHeader.indexOf(h) === -1) {
        existingHeader.push(h);
        headerChanged = true;
      }
    });
    if (headerChanged) {
      sheet.getRange(1, 1, 1, existingHeader.length).setValues([existingHeader]);
      // 重新读取（列数变了）
      existing = sheet.getRange(1, 1, lastRow, existingHeader.length).getValues();
    }

    // 列名 → 索引
    const colIdx = {};
    existingHeader.forEach((h, i) => { colIdx[h] = i; });

    // key → existing 行号（0-indexed in existing[]）
    // 关键：日期等列被 Sheets 自动转成 Date 对象，需归一化成字符串再比较
    const keyToIdx = {};
    for (let r = 1; r < existing.length; r++) {
      const key = keyCols.map(k => _toKeyVal(existing[r][colIdx[k]])).join("|");
      keyToIdx[key] = r;
    }

    let updated = 0;
    let appended = 0;
    const toAppend = [];

    rows.forEach(row => {
      const rowObj = {};
      headers.forEach((h, i) => { rowObj[h] = row[i]; });
      const key = keyCols.map(k => _toKeyVal(rowObj[k])).join("|");

      if (key in keyToIdx) {
        const idx = keyToIdx[key];
        const sheetRow = idx + 1; // 1-indexed
        const target = existing[idx];
        let dirty = false;
        headers.forEach((h, i) => {
          const v = row[i];
          if (v !== "" && v !== null && v !== undefined) {
            if (target[colIdx[h]] !== v) {
              target[colIdx[h]] = v;
              dirty = true;
            }
          }
        });
        if (dirty) {
          sheet.getRange(sheetRow, 1, 1, existingHeader.length).setValues([target]);
          updated++;
        }
      } else {
        const newRow = new Array(existingHeader.length).fill("");
        headers.forEach((h, i) => { newRow[colIdx[h]] = row[i]; });
        toAppend.push(newRow);
        appended++;
      }
    });

    if (toAppend.length > 0) {
      sheet.getRange(sheet.getLastRow() + 1, 1, toAppend.length, existingHeader.length).setValues(toAppend);
    }

    return _resp({status: "ok", sheet: sheetName, updated: updated, appended: appended});
  } catch (err) {
    return _resp({status: "error", message: String(err)});
  }
}

function doGet() {
  return _resp({status: "ok", message: "use POST"});
}

function _resp(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

/**
 * 把单元格值归一化成稳定的字符串，用于 keyCols 匹配。
 * 关键：Sheets 把 "2026/4/27" 这种字符串自动转成 Date 存储，
 *       读回时是 Date 对象，必须先格式化成同样的 yyyy/M/d 才能匹配传入字符串。
 */
function _toKeyVal(v) {
  if (v === null || v === undefined || v === "") return "";
  if (Object.prototype.toString.call(v) === "[object Date]") {
    return Utilities.formatDate(v, "Asia/Shanghai", "yyyy/M/d");
  }
  return String(v).trim();
}
