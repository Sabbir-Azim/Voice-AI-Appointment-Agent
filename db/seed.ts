// Seed MongoDB with sample users, doctors, and clinic knowledge documents.
//
// Run after db/indexes.ts:
//
//     pnpm db:seed
//
// Sample data lives in db/lib/data.ts. Edit there to customize.

import { MongoClient } from 'mongodb';
import { loadEnv, requireEnv } from './lib/env';
import { DOCTORS, KNOWLEDGE_INPUTS, USERS } from './lib/data';
import { embedTexts } from './lib/voyage';

const DEFAULT_DB_NAME = 'livekit_mongo_starter';

async function seedData(): Promise<void> {
  const { source } = loadEnv();
  console.info(`Loaded env from ${source}`);

  const client = new MongoClient(requireEnv('MONGODB_URI'));
  await client.connect();
  try {
    const db = client.db(process.env.MONGODB_DB || DEFAULT_DB_NAME);

    for (const name of [
      'users',
      'doctors',
      'appointments',
      'knowledge',
      'memories',
      'sessions',
    ]) {
      await db.collection(name).deleteMany({});
    }
    console.info('Cleared existing data from all collections');

    const now = new Date();

    const users = USERS.map((u) => ({ ...u, created_at: now }));
    await db.collection('users').insertMany(users);
    console.info(`Inserted ${users.length} users`);

    const doctors = DOCTORS.map((d) => ({ ...d, created_at: now }));
    await db.collection('doctors').insertMany(doctors);
    console.info(`Inserted ${doctors.length} doctors (appointments start empty)`);

    const embeddings = await embedTexts(
      KNOWLEDGE_INPUTS.map((doc) => doc.content),
      { inputType: 'document' },
    );
    const knowledgeDocs = KNOWLEDGE_INPUTS.map((doc, i) => ({
      ...doc,
      embedding: embeddings[i],
      created_at: now,
    }));
    await db.collection('knowledge').insertMany(knowledgeDocs);
    console.info(
      `Inserted ${knowledgeDocs.length} knowledge documents with embeddings`,
    );

    console.info('Seed complete.');
  } finally {
    await client.close();
  }
}

seedData().catch((err) => {
  console.error(err);
  process.exit(1);
});
