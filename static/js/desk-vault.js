/**
 * Encrypted last-note vault for the practice desk PWA.
 * AES-GCM. Key from staff secret via PBKDF2. Key stays in RAM after unlock.
 */
(function (global) {
  "use strict";

  var DB_NAME = "psycharts-desk-vault";
  var DB_VER = 3;
  var ITER = 210000;
  var LISTING_TTL_MS = 365 * 24 * 60 * 60 * 1000;
  var TEXT = new TextEncoder();
  var TEXT_OUT = new TextDecoder();

  var _db = null;
  var _key = null;

  function openDb() {
    if (_db) return Promise.resolve(_db);
    return new Promise(function (resolve, reject) {
      var req = indexedDB.open(DB_NAME, DB_VER);
      req.onupgradeneeded = function () {
        var db = req.result;
        if (!db.objectStoreNames.contains("meta")) db.createObjectStore("meta");
        if (!db.objectStoreNames.contains("notes")) db.createObjectStore("notes");
        if (!db.objectStoreNames.contains("listings")) db.createObjectStore("listings");
        if (!db.objectStoreNames.contains("clients")) db.createObjectStore("clients");
        if (!db.objectStoreNames.contains("visits")) db.createObjectStore("visits");
      };
      req.onsuccess = function () {
        _db = req.result;
        resolve(_db);
      };
      req.onerror = function () {
        reject(req.error || new Error("vault open failed"));
      };
    });
  }

  function idbGet(store, key) {
    return openDb().then(function (db) {
      return new Promise(function (resolve, reject) {
        var r = db.transaction(store, "readonly").objectStore(store).get(key);
        r.onsuccess = function () {
          resolve(r.result);
        };
        r.onerror = function () {
          reject(r.error);
        };
      });
    });
  }

  function idbDel(store, key) {
    return openDb().then(function (db) {
      return new Promise(function (resolve, reject) {
        var r = db.transaction(store, "readwrite").objectStore(store).delete(key);
        r.onsuccess = function () {
          resolve();
        };
        r.onerror = function () {
          reject(r.error);
        };
      });
    });
  }

  function idbKeys(store) {
    return openDb().then(function (db) {
      return new Promise(function (resolve, reject) {
        var r = db.transaction(store, "readonly").objectStore(store).getAllKeys();
        r.onsuccess = function () {
          resolve(r.result || []);
        };
        r.onerror = function () {
          reject(r.error);
        };
      });
    });
  }

  function hexOf(buf) {
    var b = new Uint8Array(buf);
    var s = "";
    for (var i = 0; i < b.length; i++) s += (b[i] < 16 ? "0" : "") + b[i].toString(16);
    return s;
  }

  function listingId(path) {
    var p = String(path || "/").replace(/\/+$/, "") || "/";
    return crypto.subtle.digest("SHA-256", TEXT.encode("listing:" + p)).then(function (buf) {
      return "l:" + hexOf(buf);
    });
  }

  function slimItems(items) {
    var out = [];
    (items || []).slice(0, 200).forEach(function (it) {
      if (!it || !it.name) return;
      out.push({
        name: String(it.name).slice(0, 240),
        type: it.type === "folder" ? "folder" : "file",
        size: Number(it.size) || 0,
      });
    });
    return out;
  }

  function idbPut(store, key, val) {
    return openDb().then(function (db) {
      return new Promise(function (resolve, reject) {
        var r = db.transaction(store, "readwrite").objectStore(store).put(val, key);
        r.onsuccess = function () {
          resolve();
        };
        r.onerror = function () {
          reject(r.error);
        };
      });
    });
  }

  function bufToB64(buf) {
    var bytes = new Uint8Array(buf);
    var s = "";
    for (var i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
    return btoa(s);
  }

  function b64ToBuf(b64) {
    var s = atob(b64);
    var out = new Uint8Array(s.length);
    for (var i = 0; i < s.length; i++) out[i] = s.charCodeAt(i);
    return out.buffer;
  }

  function randomBytes(n) {
    var b = new Uint8Array(n);
    crypto.getRandomValues(b);
    return b;
  }

  function deriveKey(secret, salt) {
    return crypto.subtle
      .importKey("raw", TEXT.encode(secret), "PBKDF2", false, ["deriveKey"])
      .then(function (base) {
        return crypto.subtle.deriveKey(
          { name: "PBKDF2", salt: salt, iterations: ITER, hash: "SHA-256" },
          base,
          { name: "AES-GCM", length: 256 },
          false,
          ["encrypt", "decrypt"]
        );
      });
  }

  function encryptJson(key, obj) {
    var iv = randomBytes(12);
    return crypto.subtle
      .encrypt({ name: "AES-GCM", iv: iv }, key, TEXT.encode(JSON.stringify(obj)))
      .then(function (ct) {
        return { iv: bufToB64(iv.buffer), ct: bufToB64(ct) };
      });
  }

  function decryptJson(key, pack) {
    if (!pack || !pack.iv || !pack.ct) return Promise.reject(new Error("empty"));
    return crypto.subtle
      .decrypt(
        { name: "AES-GCM", iv: new Uint8Array(b64ToBuf(pack.iv)) },
        key,
        b64ToBuf(pack.ct)
      )
      .then(function (pt) {
        return JSON.parse(TEXT_OUT.decode(pt));
      });
  }

  function clientKey(ref) {
    var id = String((ref && ref.id) || "").trim();
    var email = String((ref && ref.email) || "").trim().toLowerCase();
    if (id) return "c:" + id;
    if (email) return "e:" + email;
    return "";
  }

  function pickDiagnosis(fields) {
    fields = fields || {};
    return String(
      fields.diagnosis ||
        fields.assessment ||
        fields.dx ||
        fields.diagnoses ||
        ""
    ).trim();
  }

  function shapeNote(fields, extra) {
    fields = fields || {};
    extra = extra || {};
    return {
      dos: String(fields.dos || extra.dos || "").slice(0, 10),
      template: String(extra.template || fields.template || ""),
      diagnosis: pickDiagnosis(fields),
      medications: String(fields.medications || "").trim(),
      supplements: String(fields.supplements || "").trim(),
      recommendations: String(
        fields.recommendations || fields.plan || ""
      ).trim(),
      return_visit: String(fields.return_visit || "").trim(),
      values: fields,
      signed_at: extra.signed_at || new Date().toISOString(),
      name: extra.name || "",
    };
  }

  var vault = {
    ready: function () {
      return !!_key;
    },
    lock: function () {
      _key = null;
    },
    unlock: function (secret) {
      secret = String(secret || "").trim();
      if (!secret || !global.crypto || !crypto.subtle) {
        return Promise.reject(new Error("vault unavailable"));
      }
      return openDb()
        .then(function () {
          return idbGet("meta", "kdf");
        })
        .then(function (meta) {
          if (!meta || !meta.salt) {
            var salt = randomBytes(16);
            return deriveKey(secret, salt).then(function (key) {
              return encryptJson(key, { ok: 1, v: 1 }).then(function (ver) {
                return idbPut("meta", "kdf", {
                  salt: bufToB64(salt.buffer),
                  iter: ITER,
                  ver: ver,
                }).then(function () {
                  _key = key;
                });
              });
            });
          }
          var salt = new Uint8Array(b64ToBuf(meta.salt));
          return deriveKey(secret, salt).then(function (key) {
            return decryptJson(key, meta.ver).then(function () {
              _key = key;
              vault.pruneListings();
            });
          });
        });
    },
    putNote: function (ref, fields, extra) {
      if (!_key) return Promise.resolve(false);
      var k = clientKey(ref);
      if (!k) return Promise.resolve(false);
      var rec = shapeNote(fields, extra);
      return encryptJson(_key, rec)
        .then(function (pack) {
          return idbPut("notes", k, pack);
        })
        .then(function () {
          var email = String((ref && ref.email) || "").trim().toLowerCase();
          if (email && k !== "e:" + email) return idbPut("notes", "e:" + email, { alias: k });
        })
        .then(function () {
          return true;
        })
        .catch(function () {
          return false;
        });
    },
    getNote: function (ref) {
      if (!_key) return Promise.resolve(null);
      var k = clientKey(ref);
      if (!k) return Promise.resolve(null);
      return idbGet("notes", k)
        .then(function (pack) {
          if (pack && pack.alias) return idbGet("notes", pack.alias);
          return pack;
        })
        .then(function (pack) {
          if (!pack || !pack.ct) return null;
          return decryptJson(_key, pack);
        })
        .catch(function () {
          return null;
        });
    },
    putListing: function (path, items) {
      if (!_key) return Promise.resolve(false);
      var rec = {
        path: String(path || "/").replace(/\/+$/, "") || "/",
        items: slimItems(items),
      };
      return listingId(rec.path)
        .then(function (id) {
          return encryptJson(_key, rec).then(function (pack) {
            return idbPut("listings", id, { pack: pack, seen: Date.now() });
          });
        })
        .then(function () {
          return true;
        })
        .catch(function () {
          return false;
        });
    },
    getListing: function (path) {
      if (!_key) return Promise.resolve(null);
      var p = String(path || "/").replace(/\/+$/, "") || "/";
      return listingId(p)
        .then(function (id) {
          return idbGet("listings", id).then(function (row) {
            if (!row || !row.pack) return null;
            if (row.seen && Date.now() - row.seen > LISTING_TTL_MS) {
              idbDel("listings", id).catch(function () {});
              return null;
            }
            return decryptJson(_key, row.pack).then(function (rec) {
              idbPut("listings", id, { pack: row.pack, seen: Date.now() }).catch(function () {});
              return rec;
            });
          });
        })
        .catch(function () {
          return null;
        });
    },
    putClientPaths: function (ref, paths) {
      if (!_key) return Promise.resolve(false);
      var k = clientKey(ref);
      if (!k) return Promise.resolve(false);
      var rec = {
        chart_path: String((paths && paths.chart_path) || ""),
        billing_path: String((paths && paths.billing_path) || ""),
        folder_path: String((paths && paths.folder_path) || ""),
      };
      return encryptJson(_key, rec)
        .then(function (pack) {
          return idbPut("listings", "p:" + k, { pack: pack, seen: Date.now() });
        })
        .then(function () {
          return true;
        })
        .catch(function () {
          return false;
        });
    },
    getClientPaths: function (ref) {
      if (!_key) return Promise.resolve(null);
      var k = clientKey(ref);
      if (!k) return Promise.resolve(null);
      return idbGet("listings", "p:" + k)
        .then(function (row) {
          if (!row || !row.pack) return null;
          if (row.seen && Date.now() - row.seen > LISTING_TTL_MS) {
            idbDel("listings", "p:" + k).catch(function () {});
            return null;
          }
          return decryptJson(_key, row.pack).then(function (rec) {
            idbPut("listings", "p:" + k, { pack: row.pack, seen: Date.now() }).catch(function () {});
            return rec;
          });
        })
        .catch(function () {
          return null;
        });
    },
    pruneListings: function () {
      if (!_key) return Promise.resolve(0);
      var cutoff = Date.now() - LISTING_TTL_MS;
      return idbKeys("listings").then(function (keys) {
        var n = 0;
        return Promise.all(
          (keys || []).map(function (k) {
            return idbGet("listings", k).then(function (row) {
              if (row && row.seen && row.seen < cutoff) {
                n += 1;
                return idbDel("listings", k);
              }
            });
          })
        ).then(function () {
          return n;
        });
      }).catch(function () {
        return 0;
      });
    },
    putClient: function (client) {
      if (!_key) return Promise.resolve(false);
      client = client || {};
      var k = clientKey(client);
      if (!k) {
        k = "c:local-" + Date.now().toString(36) + "-" + Math.floor(Math.random() * 1e9).toString(36);
      }
      var rec = {
        id: String(client.id || (k.indexOf("c:") === 0 ? k.slice(2) : "")),
        name: String(client.name || "").slice(0, 200),
        email: String(client.email || "").slice(0, 200),
        phone: String(client.phone || "").slice(0, 60),
        notes: String(client.notes || "").slice(0, 4000),
        source: String(client.source || ""),
        updated: new Date().toISOString(),
      };
      return encryptJson(_key, rec)
        .then(function (pack) {
          return idbPut("clients", k, { pack: pack, updated: Date.now() });
        })
        .then(function () {
          return k;
        })
        .catch(function () {
          return false;
        });
    },
    getClient: function (ref) {
      if (!_key) return Promise.resolve(null);
      var k = clientKey(ref);
      if (!k) return Promise.resolve(null);
      return idbGet("clients", k)
        .then(function (row) {
          if (!row || !row.pack) return null;
          return decryptJson(_key, row.pack);
        })
        .catch(function () {
          return null;
        });
    },
    listClients: function (query) {
      if (!_key) return Promise.resolve([]);
      var q = String(query || "").trim().toLowerCase();
      return idbKeys("clients")
        .then(function (keys) {
          return Promise.all(
            (keys || []).map(function (k) {
              return idbGet("clients", k).then(function (row) {
                if (!row || !row.pack) return null;
                return decryptJson(_key, row.pack).then(function (rec) {
                  rec._k = k;
                  return rec;
                });
              });
            })
          );
        })
        .then(function (recs) {
          var out = [];
          (recs || []).forEach(function (rec) {
            if (!rec) return;
            if (q) {
              var hay = (rec.name + " " + rec.email).toLowerCase();
              if (hay.indexOf(q) < 0) return;
            }
            out.push(rec);
          });
          out.sort(function (a, b) {
            return String(a.name || "").localeCompare(String(b.name || ""));
          });
          return out.slice(0, 200);
        })
        .catch(function () {
          return [];
        });
    },
    putVisit: function (visit) {
      if (!_key) return Promise.resolve(false);
      visit = visit || {};
      var id = String(visit.id || "").trim();
      if (!id) {
        id = "local-" + Date.now().toString(36) + "-" + Math.floor(Math.random() * 1e9).toString(36);
      }
      var rec = {
        id: id,
        email: String(visit.email || "").slice(0, 200),
        name: String(visit.name || "").slice(0, 200),
        event_type: String(visit.event_type || "").slice(0, 60),
        start: String(visit.start || "").slice(0, 48),
        duration_minutes: visit.duration_minutes || null,
        sms_consent: !!visit.sms_consent,
        notes: String(visit.notes || "").slice(0, 4000),
        envelope_id: String(visit.envelope_id || ""),
        updated: new Date().toISOString(),
      };
      return encryptJson(_key, rec)
        .then(function (pack) {
          return idbPut("visits", "v:" + id, { pack: pack, updated: Date.now() });
        })
        .then(function () {
          return "v:" + id;
        })
        .catch(function () {
          return false;
        });
    },
    getVisit: function (id) {
      if (!_key) return Promise.resolve(null);
      var v = String(id || "").trim();
      if (!v) return Promise.resolve(null);
      return idbGet("visits", "v:" + v)
        .then(function (row) {
          if (!row || !row.pack) return null;
          return decryptJson(_key, row.pack);
        })
        .catch(function () {
          return null;
        });
    },
    exportSnapshot: function () {
      // Whole-vault encrypted dump. Rows stay in their vault-key packs; the
      // dump itself is encrypted again with the same key. Never server ECDH.
      if (!_key) return Promise.reject(new Error("vault locked"));
      var stores = ["notes", "listings", "clients", "visits"];
      var dump = { exported: new Date().toISOString(), stores: {} };
      var chain = Promise.resolve();
      stores.forEach(function (sname) {
        chain = chain.then(function () {
          return idbKeys(sname).then(function (keys) {
            dump.stores[sname] = {};
            return Promise.all(
              (keys || []).map(function (k) {
                return idbGet(sname, k).then(function (row) {
                  if (row != null) dump.stores[sname][k] = row;
                });
              })
            );
          });
        });
      });
      return chain
        .then(function () {
          return encryptJson(_key, dump);
        })
        .then(function (pack) {
          return { v: 1, wrap: "vault", alg: "A256GCM", pack: pack };
        });
    },
    importSnapshot: function (snap) {
      if (!_key) return Promise.reject(new Error("vault locked"));
      if (!snap || snap.wrap !== "vault" || !snap.pack) {
        return Promise.reject(new Error("not a vault snapshot"));
      }
      return decryptJson(_key, snap.pack).then(function (dump) {
        var stores = (dump && dump.stores) || {};
        var rows = 0;
        var chain = Promise.resolve();
        ["notes", "listings", "clients", "visits"].forEach(function (sname) {
          var bag = stores[sname] || {};
          Object.keys(bag).forEach(function (k) {
            chain = chain.then(function () {
              return idbPut(sname, k, bag[k]).then(function () {
                rows += 1;
              });
            });
          });
        });
        return chain.then(function () {
          return { ok: true, rows: rows };
        });
      });
    },
    clientKey: clientKey,
    shapeNote: shapeNote,
  };

  global.PsychartsVault = vault;
})(window);
