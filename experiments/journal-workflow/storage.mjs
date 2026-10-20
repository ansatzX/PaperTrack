import fs from 'node:fs/promises';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
export async function readJson(file, fallback = null) {
  try {return JSON.parse(await fs.readFile(file,'utf8'));}
  catch(error) {if(error.code === 'ENOENT') return fallback; throw error;}
}
export async function atomicJson(file, value) {
  await fs.mkdir(path.dirname(file),{recursive:true,mode:0o700});
  const temp = `${file}.${randomUUID()}.tmp`;
  const handle = await fs.open(temp,'wx',0o600);
  try {await handle.writeFile(JSON.stringify(value,null,2)); await handle.sync();} finally {await handle.close();}
  await fs.rename(temp,file);
}
export const pause = ms => new Promise(resolve=>setTimeout(resolve,ms));
