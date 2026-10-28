'use strict';
// Minimal ZIP reader (store + deflate), zero dependencies. Enough for Alipay
// exports: one CSV entry per archive, no ZIP64, no encryption, no multi-disk.
const zlib = require('node:zlib');

const SIG_EOCD = 0x06054b50;
const SIG_CEN = 0x02014b50;
const SIG_LOC = 0x04034b50;

class ZipError extends Error {
  constructor(message) {
    super(message);
    this.name = 'ZipError';
  }
}

function findEocd(buf) {
  const min = Math.max(0, buf.length - 65557); // max comment 65535 + 22
  for (let i = buf.length - 22; i >= min; i--) {
    if (buf.readUInt32LE(i) === SIG_EOCD) {
      const commentLen = buf.readUInt16LE(i + 20);
      if (i + 22 + commentLen === buf.length || i + 22 + commentLen <= buf.length) return i;
    }
  }
  return -1;
}

function readZipEntries(buf) {
  if (!Buffer.isBuffer(buf)) buf = Buffer.from(buf);
  if (buf.length < 22) throw new ZipError('file too small to be a zip');
  const eocd = findEocd(buf);
  if (eocd < 0) throw new ZipError('end of central directory not found');

  const diskNo = buf.readUInt16LE(eocd + 4);
  if (diskNo !== 0) throw new ZipError('multi-disk archives are not supported');
  const entryCount = buf.readUInt16LE(eocd + 10);
  const cdOffset = buf.readUInt32LE(eocd + 16);

  if (entryCount === 0xffff || cdOffset === 0xffffffff) throw new ZipError('zip64 archives are not supported');
  if (cdOffset > buf.length) throw new ZipError('central directory offset out of range');

  const entries = [];
  let p = cdOffset;
  for (let i = 0; i < entryCount; i++) {
    if (p + 46 > buf.length) throw new ZipError('truncated central directory');
    if (buf.readUInt32LE(p) !== SIG_CEN) throw new ZipError('bad central directory signature');
    const flags = buf.readUInt16LE(p + 8);
    const method = buf.readUInt16LE(p + 10);
    const crc32 = buf.readUInt32LE(p + 16);
    const compSize = buf.readUInt32LE(p + 20);
    const size = buf.readUInt32LE(p + 24);
    const nameLen = buf.readUInt16LE(p + 28);
    const extraLen = buf.readUInt16LE(p + 30);
    const commentLen = buf.readUInt16LE(p + 32);
    const localOffset = buf.readUInt32LE(p + 42);
    if (p + 46 + nameLen > buf.length) throw new ZipError('truncated central directory name');
    const name = buf.toString(flags & 0x800 ? 'utf8' : 'latin1', p + 46, p + 46 + nameLen);
    entries.push({
      name,
      method,
      flags,
      crc32,
      compressedSize: compSize,
      size,
      localOffset,
      enc: flags & 0x1 ? 'encrypted' : null,
    });
    p += 46 + nameLen + extraLen + commentLen;
  }

  for (const e of entries) {
    if (e.enc) throw new ZipError(`entry ${e.name} is encrypted`);
    const lo = e.localOffset;
    if (lo + 30 > buf.length) throw new ZipError(`bad local header offset for ${e.name}`);
    if (buf.readUInt32LE(lo) !== SIG_LOC) throw new ZipError(`bad local header signature for ${e.name}`);
    const nameLen = buf.readUInt16LE(lo + 26);
    const extraLen = buf.readUInt16LE(lo + 28);
    const dataStart = lo + 30 + nameLen + extraLen;
    if (dataStart + e.compressedSize > buf.length) throw new ZipError(`truncated data for ${e.name}`);
    const raw = buf.subarray(dataStart, dataStart + e.compressedSize);
    let data;
    if (e.method === 0) {
      data = Buffer.from(raw);
    } else if (e.method === 8) {
      try {
        data = zlib.inflateRawSync(raw);
      } catch (err) {
        throw new ZipError(`inflate failed for ${e.name}: ${err.message}`);
      }
    } else {
      throw new ZipError(`unsupported compression method ${e.method} for ${e.name}`);
    }
    if (data.length !== e.size) throw new ZipError(`size mismatch for ${e.name}`);
    e.data = data;
  }
  return entries;
}

function isZip(buf) {
  return Buffer.isBuffer(buf) && buf.length > 3 && buf[0] === 0x50 && buf[1] === 0x4b && buf[2] === 0x03 && buf[3] === 0x04;
}

function pickCsvEntry(entries) {
  const csvs = entries.filter((e) => /\.csv$/i.test(e.name) && e.size > 0);
  if (!csvs.length) return null;
  // Prefer the largest CSV (extra files like readme.txt may exist).
  return csvs.sort((a, b) => b.size - a.size)[0];
}

module.exports = { readZipEntries, isZip, pickCsvEntry, ZipError };
