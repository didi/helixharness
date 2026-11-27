'use strict'

const fs = require('fs')
const zlib = require('zlib')

// An evidence audit must bound decompression. Larger captures get an explicit
// size diagnostic; this does not classify them as corrupt or visually invalid.
const MAX_FILE_BYTES = 32 * 1024 * 1024
const MAX_DECODED_BYTES = 128 * 1024 * 1024
const crcTable = Array.from({ length: 256 }, (_, index) => {
  let crc = index
  for (let bit = 0; bit < 8; bit += 1) crc = (crc & 1) ? 0xedb88320 ^ (crc >>> 1) : crc >>> 1
  return crc >>> 0
})

function crc32(bytes) {
  let crc = 0xffffffff
  for (const byte of bytes) crc = crcTable[(crc ^ byte) & 255] ^ (crc >>> 8)
  return (crc ^ 0xffffffff) >>> 0
}

function validatePngFile(file) {
  if (fs.statSync(file).size > MAX_FILE_BYTES) throw new Error('PNG evidence exceeds the 32 MiB compressed audit limit; use a smaller supported capture')
  const bytes = fs.readFileSync(file)
  const invalid = () => { throw new Error('PNG evidence is malformed or cannot be decompressed') }
  if (bytes.length < 57 || bytes.subarray(0, 8).toString('hex') !== '89504e470d0a1a0a') invalid()
  let offset = 8
  let header = null
  let ended = false
  let idatEnded = false
  let palette = false
  let paletteEntries = 0
  const compressed = []
  while (offset < bytes.length) {
    if (offset + 12 > bytes.length || ended) invalid()
    const length = bytes.readUInt32BE(offset)
    const end = offset + 12 + length
    if (end > bytes.length) invalid()
    const kind = bytes.toString('latin1', offset + 4, offset + 8)
    if (!/^[A-Za-z]{4}$/.test(kind) || kind[2] !== kind[2].toUpperCase()) invalid()
    const data = bytes.subarray(offset + 8, end - 4)
    if (crc32(bytes.subarray(offset + 4, end - 4)) !== bytes.readUInt32BE(end - 4)) invalid()
    if (!header && kind !== 'IHDR') invalid()
    if (kind === 'IHDR') {
      if (header || length !== 13) invalid()
      const width = data.readUInt32BE(0)
      const height = data.readUInt32BE(4)
      const depth = data[8]
      const color = data[9]
      const channels = { 0: 1, 2: 3, 3: 1, 4: 2, 6: 4 }[color]
      const depths = { 0: [1, 2, 4, 8, 16], 2: [8, 16], 3: [1, 2, 4, 8], 4: [8, 16], 6: [8, 16] }[color]
      if (!width || !height || width > 0x7fffffff || height > 0x7fffffff || !channels || !depths.includes(depth) || data[10] !== 0 || data[11] !== 0 || data[12] > 1) invalid()
      header = { width, height, depth, color, channels, interlace: data[12] }
    } else if (kind === 'PLTE') {
      if (palette || compressed.length || !length || length % 3 || length > 768 || [0, 4].includes(header.color)) invalid()
      palette = true
      paletteEntries = length / 3
      if (header.color === 3 && paletteEntries > 2 ** header.depth) invalid()
    } else if (kind === 'IDAT') {
      if (idatEnded || (header.color === 3 && !palette)) invalid()
      compressed.push(data)
    } else if (kind === 'IEND') {
      if (length || !compressed.length) invalid()
      ended = true
    } else {
      // Unknown critical chunks cannot be decoded by the PNG contract.
      if (kind[0] === kind[0].toUpperCase()) invalid()
      if (compressed.length) idatEnded = true
    }
    offset = end
  }
  if (!ended) invalid()
  const passes = header.interlace
    ? [[0, 0, 8, 8], [4, 0, 8, 8], [0, 4, 4, 8], [2, 0, 4, 4], [0, 2, 2, 4], [1, 0, 2, 2], [0, 1, 1, 2]]
    : [[0, 0, 1, 1]]
  const rows = []
  let expectedBytes = 0
  for (const [x, y, dx, dy] of passes) {
    const width = Math.max(0, Math.ceil((header.width - x) / dx))
    const height = Math.max(0, Math.ceil((header.height - y) / dy))
    if (!width || !height) continue
    const rowBytes = Math.ceil(width * header.channels * header.depth / 8)
    expectedBytes += (rowBytes + 1) * height
    rows.push({ rowBytes, height })
  }
  if (!Number.isSafeInteger(expectedBytes) || expectedBytes > MAX_DECODED_BYTES) throw new Error('PNG evidence exceeds the 128 MiB decoded audit limit; use a smaller supported capture')
  const data = Buffer.concat(compressed)
  let decoded
  try {
    const inflated = zlib.inflateSync(data, { maxOutputLength: Math.max(1, expectedBytes), info: true })
    if (inflated.engine.bytesWritten !== data.length) invalid()
    decoded = inflated.buffer
  } catch { invalid() }
  if (decoded.length !== expectedBytes) invalid()
  offset = 0
  for (const { rowBytes, height } of rows) {
    for (let row = 0; row < height; row += 1) {
      if (decoded[offset] > 4) invalid()
      offset += rowBytes + 1
    }
  }
  return bytes
}

module.exports = { validatePngFile, MAX_FILE_BYTES, MAX_DECODED_BYTES }
