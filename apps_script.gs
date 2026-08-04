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
 *
 * 可选字段：
 *   - appendMode: "tailOnly" → 未命中的行只有当其「日期」>= 表内现有最大日期时才追加，
 *     否则计入 skipped 丢弃。保证日期只能在表尾累加，绝不在末尾插入乱序的历史日期。
 *   - deleteKeys: [[keyVal,...], ...] → 按 keyCols 精确匹配删除整行（自底向上），
 *     发生在 upsert 之前；配 rows:[] 可作纯清理调用。
 *
 * 追加位置：若 tab 里有 setupDividendStats 的表尾统计行（日期列=「最新-历史最高」），
 * 新行一律插在统计行之前，让统计行永远钉在最新数据之后；无统计行的 tab 仍是纯表尾追加。
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

    const appendMode = data.appendMode || "";
    const deleteKeys = data.deleteKeys || [];

    // 删除阶段：按 keyCols 精确匹配整行删除（自底向上保证行号有效）
    let deleted = 0;
    if (deleteKeys.length > 0 && sheet.getLastRow() > 1) {
      const all = sheet.getRange(1, 1, sheet.getLastRow(), sheet.getLastColumn()).getValues();
      const hci = {};
      all[0].forEach((h, i) => { hci[h] = i; });
      const wanted = {};
      deleteKeys.forEach(k => { wanted[k.map(v => String(v).trim()).join("|")] = true; });
      for (let r = all.length - 1; r >= 1; r--) {
        const key = keyCols.map(kc => _toKeyVal(all[r][hci[kc]])).join("|");
        if (wanted[key]) { sheet.deleteRow(r + 1); deleted++; }
      }
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

    // tailOnly 守卫：算出表内现有最大日期
    let maxDateMs = -Infinity;
    const dateColIdx = colIdx["日期"];
    if (appendMode === "tailOnly" && dateColIdx !== undefined) {
      for (let r = 1; r < existing.length; r++) {
        const t = _toDateMs(existing[r][dateColIdx]);
        if (t !== null && t > maxDateMs) maxDateMs = t;
      }
    }

    let updated = 0;
    let appended = 0;
    let skipped = 0;
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
        if (appendMode === "tailOnly" && dateColIdx !== undefined) {
          const t = _toDateMs(rowObj["日期"]);
          if (t === null || t < maxDateMs) { skipped++; return; }
          if (t > maxDateMs) maxDateMs = t;
        }
        const newRow = new Array(existingHeader.length).fill("");
        headers.forEach((h, i) => { newRow[colIdx[h]] = row[i]; });
        toAppend.push(newRow);
        appended++;
      }
    });

    if (toAppend.length > 0) {
      // 统计行钉在表尾：有统计行的 tab 把新行插到统计行之前，其余 tab 原样表尾追加
      let statRow = -1;
      if (dateColIdx !== undefined) {
        for (let r = 1; r < existing.length; r++) {
          if (_toKeyVal(existing[r][dateColIdx]) === "最新-历史最高") { statRow = r + 1; break; }
        }
      }
      if (statRow > 0) {
        // insertRowsAfter(上一行) 让新行继承数据行格式；insertRowsBefore 会继承统计行的加粗
        sheet.insertRowsAfter(statRow - 1, toAppend.length);
        sheet.getRange(statRow, 1, toAppend.length, existingHeader.length).setValues(toAppend);
      } else {
        sheet.getRange(sheet.getLastRow() + 1, 1, toAppend.length, existingHeader.length).setValues(toAppend);
      }
    }

    return _resp({status: "ok", sheet: sheetName, updated: updated, appended: appended, skipped: skipped, deleted: deleted});
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
 * 一次性/可重跑：把「指数价格」总表按代码拆到 7 个分表 tab。
 * 在 Apps Script 编辑器里选中本函数点「运行」即可（无需重新部署）。
 * 分表不存在则创建；已存在则 clearContents 后全量重建（幂等）。
 * tab 名与 fetch_index_valuation.py 的 INDEX_TABS 保持一致。
 * 注意：重跑本函数会清掉 setupDividendStats() 装的表尾统计行，
 *       跑完必须再跑一次 setupDividendStats() 复位（它会自愈重装）。
 */
function migrateSplitTabs() {
  const SPLIT_TABS = {
    "SH000300": "沪深300",
    "SH000905": "中证500",
    "SH000852": "中证1000",
    "SZ399006": "创业板",
    "SH000015": "上证红利",
    "SH000922": "中证红利",
    "CSIH30269": "红利低波"
  };
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const src = ss.getSheetByName("指数价格");
  if (!src) throw new Error("总表「指数价格」不存在");
  const data = src.getDataRange().getValues();
  const header = data[0];
  const codeIdx = header.indexOf("代码");
  if (codeIdx === -1) throw new Error("总表缺少「代码」列");

  Object.keys(SPLIT_TABS).forEach(code => {
    const tabName = SPLIT_TABS[code];
    let sheet = ss.getSheetByName(tabName);
    if (!sheet) sheet = ss.insertSheet(tabName);
    sheet.clearContents();
    const rows = data.slice(1).filter(r => String(r[codeIdx]).trim() === code);
    sheet.getRange(1, 1, 1, header.length).setValues([header]);
    if (rows.length > 0) {
      sheet.getRange(2, 1, rows.length, header.length).setValues(rows);
    }
    // getValues 读出的百分比是纯数字，写回新 tab 会丢显示格式，必须显式补格式
    if (rows.length > 0) {
      sheet.getRange(2, 1, rows.length, 1).setNumberFormat("yyyy/m/d");
      ["PE 百分位", "PB 百分位", "股息率", "有知有行股息率"].forEach(function(colName) {
        const ci = header.indexOf(colName);
        if (ci !== -1) sheet.getRange(2, ci + 1, rows.length, 1).setNumberFormat("0.00%");
      });
    }
    Logger.log(tabName + ": " + rows.length + " 行");
  });
}

/**
 * 一次性/可重跑：给 4 个红利分表（上证红利/中证红利/红利低波/红利低波100）装
 * 「最新 vs 历史极值」统计行（钉在表尾、最新数据之后）+ 全列最大/最小值自动高亮。
 * 在 Apps Script 编辑器里选中本函数点「运行」即可；但配套的「新行插到统计行之前」
 * 逻辑在 doPost 里，doPost 有改动时必须重新部署 Web App 才对线上生效。
 *
 * 做的事（幂等，可反复运行修复/扩列；会自动把旧版装在第 2/3 行的统计行迁到表尾）：
 *   1. 最后一行数据之后放两行常驻统计行：
 *        「最新-历史最高」= 该列最新一天的值 − 全列历史最高（负数 = 比最高点低多少个百分点）
 *        「最新-历史最低」= 该列最新一天的值 − 全列历史最低（正数 = 比最低点高多少个百分点）
 *      覆盖列：股息率 / 有知有行股息率 / 理杏仁（表头存在就装，空列显示空、来数自动生效）。
 *      公式区间 = 列$2 : INDEX(列,ROW()-k)，即「第 2 行到统计行上一行」：
 *      doPost 把新行插到统计行之前时统计行下移、ROW() 自动跟随，区间永远恰好盖住
 *      全部数据且不含统计行自身（避免循环引用/自我污染）。
 *      统计行日期列是文字标签、没有代码键，webhook 的 upsert/删除永远不会命中它们。
 *   2. 每列装两条条件格式：全列最大值黄底、最小值蓝底，数据一变自动重算重标。
 *      极值用 MAXIFS/MINIFS 按「A 列是日期(数值>0)」过滤，统计行和空行既不参与
 *      极值计算也不会被高亮。规则区间铺到第 5000 行并把网格先扩到 5000 行。
 *   3. 冻结第 1 行表头（统计行随表尾滚动，不再冻结）。
 */
function setupDividendStats() {
  const TABS = ["上证红利", "中证红利", "红利低波", "红利低波100"];
  const YIELD_COLS = ["股息率", "有知有行股息率", "理杏仁"];
  const LABEL_HIGH = "最新-历史最高";
  const LABEL_LOW = "最新-历史最低";
  const GRID_ROWS = 5000; // 条件格式规则铺到的行数
  const YELLOW = "#ffff00";
  const BLUE = "#6fa8dc";

  const ss = SpreadsheetApp.getActiveSpreadsheet();
  TABS.forEach(function(tabName) {
    const sheet = ss.getSheetByName(tabName);
    if (!sheet) {
      Logger.log(tabName + ": tab 不存在，跳过");
      return;
    }

    // 0) 旧版布局（统计行在第 2/3 行）：先拆掉，数据重新从第 2 行开始
    if (String(sheet.getRange(2, 1).getValue()).trim() === LABEL_HIGH) {
      sheet.deleteRows(2, 2);
    }
    sheet.setFrozenRows(1);

    // 网格扩到 GRID_ROWS，保证日更插入的新行始终落在条件格式区间内
    if (sheet.getMaxRows() < GRID_ROWS) {
      sheet.insertRowsAfter(sheet.getMaxRows(), GRID_ROWS - sheet.getMaxRows());
    }

    // 1) 定位已有的表尾统计行；没有就紧跟最后一行数据新建
    const lastRow = sheet.getLastRow();
    let statRow = -1;
    if (lastRow >= 2) {
      const aVals = sheet.getRange(2, 1, lastRow - 1, 1).getValues();
      for (let i = 0; i < aVals.length; i++) {
        if (String(aVals[i][0]).trim() === LABEL_HIGH) { statRow = i + 2; break; }
      }
    }
    if (statRow === -1) statRow = lastRow + 1;
    sheet.getRange(statRow, 1, 2, sheet.getMaxColumns()).clearFormat();
    sheet.getRange(statRow, 1).setValue(LABEL_HIGH);
    sheet.getRange(statRow + 1, 1).setValue(LABEL_LOW);
    sheet.getRange(statRow, 1, 2, 1).setFontWeight("bold");
    sheet.getRange(statRow, 1).setNote("该列最新一天的股息率 − 全部历史最高值（负数 = 比最高点低多少个百分点）");
    sheet.getRange(statRow + 1, 1).setNote("该列最新一天的股息率 − 全部历史最低值（正数 = 比最低点高多少个百分点）");

    const header = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];

    // 2) 逐股息率列装统计公式，并重建极值高亮规则（先摘掉旧装的，保留无关规则）
    const keptRules = sheet.getConditionalFormatRules().filter(function(r) {
      return !_isExtremeRule(r);
    });
    const newRules = [];
    YIELD_COLS.forEach(function(colName) {
      const ci = header.indexOf(colName);
      if (ci === -1) {
        Logger.log(tabName + " / " + colName + ": 表头无此列，跳过");
        return;
      }
      const n = ci + 1;
      const L = _colLetter(n);
      for (let k = 0; k < 2; k++) {
        // 区间兜到统计行上一行为止：第 1 统计行 ROW()-1、第 2 统计行 ROW()-2
        const rng = L + "$2:INDEX(" + L + ":" + L + ",ROW()-" + (k + 1) + ")";
        // 区间最后一个非空值 = 最新一天；范围运算必须套 ARRAYFORMULA，裸 LOOKUP 会被隐式交集打成 #N/A
        const latest = 'ARRAYFORMULA(LOOKUP(2,1/(' + rng + '<>""),' + rng + '))';
        const agg = k === 0 ? "MAX" : "MIN";
        sheet.getRange(statRow + k, n).setFormula("=IFERROR(" + latest + "-" + agg + "(" + rng + '),"")');
      }
      sheet.getRange(statRow, n, 2, 1).setNumberFormat("0.00%");

      // 高亮只认「A 列是日期」的数据行；MAXIFS/MINIFS 按 A>0 把统计行/空行排除出极值
      const ruleRange = sheet.getRange(L + "2:" + L + sheet.getMaxRows());
      newRules.push(SpreadsheetApp.newConditionalFormatRule()
        .whenFormulaSatisfied('=AND(ISNUMBER($A2),' + L + '2<>"",' + L + '2=MAXIFS(' + L + '$2:' + L + ',$A$2:$A,">0"))')
        .setBackground(YELLOW).setRanges([ruleRange]).build());
      newRules.push(SpreadsheetApp.newConditionalFormatRule()
        .whenFormulaSatisfied('=AND(ISNUMBER($A2),' + L + '2<>"",' + L + '2=MINIFS(' + L + '$2:' + L + ',$A$2:$A,">0"))')
        .setBackground(BLUE).setRanges([ruleRange]).build());
      Logger.log(tabName + " / " + colName + "（" + L + " 列）: 表尾统计行 + 高亮已装");
    });
    sheet.setConditionalFormatRules(keptRules.concat(newRules));
  });
}

/** 识别 setupDividendStats 装的极值高亮规则（新版含 MAXIFS/MINIFS，旧版含 "$4:"+MAX(/MIN(），幂等重装时先摘除。 */
function _isExtremeRule(rule) {
  const bc = rule.getBooleanCondition();
  if (!bc || bc.getCriteriaType() !== SpreadsheetApp.BooleanCriteria.CUSTOM_FORMULA) return false;
  const vals = bc.getCriteriaValues();
  const f = vals && vals.length ? String(vals[0]) : "";
  if (f.indexOf("MAXIFS(") !== -1 || f.indexOf("MINIFS(") !== -1) return true;
  return f.indexOf("$4:") !== -1 && (f.indexOf("MAX(") !== -1 || f.indexOf("MIN(") !== -1);
}

/** 1-based 列号 → 字母（A..Z, AA..） */
function _colLetter(n) {
  let s = "";
  while (n > 0) {
    s = String.fromCharCode(65 + ((n - 1) % 26)) + s;
    n = Math.floor((n - 1) / 26);
  }
  return s;
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

/**
 * 把日期单元格值（Date 对象或 "yyyy/M/d" 字符串）归一化成毫秒时间戳；解析失败返回 null。
 * tailOnly 守卫用它比较日期先后。
 */
function _toDateMs(v) {
  if (v === null || v === undefined || v === "") return null;
  if (Object.prototype.toString.call(v) === "[object Date]") return v.getTime();
  const m = String(v).trim().match(/^(\d{4})\/(\d{1,2})\/(\d{1,2})$/);
  if (!m) return null;
  return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])).getTime();
}
