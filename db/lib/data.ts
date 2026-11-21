// Shared sample data for db/seed.ts.
//
// Pure data, no driver code, no I/O. To customize the clinic (doctors,
// hours, FAQs), edit this file and re-run `pnpm db:seed`.

export interface UserSeed {
  user_id: string;
  name: string;
  email: string;
  preferences: { language: string; timezone: string };
}

// Working hours are in the clinic's local time zone (CLINIC_TIMEZONE in
// agent-py/.env.local). working_days uses ISO weekdays: 1 = Monday ... 7 = Sunday.
export interface DoctorSeed {
  doctor_id: string;
  name: string;
  specialty: string;
  working_days: number[];
  start_hour: number;
  end_hour: number;
  slot_minutes: number;
}

export interface KnowledgeSeed {
  title: string;
  content: string;
  category: string;
}

export const USERS: UserSeed[] = [
  {
    user_id: 'user_1',
    name: 'Jordan',
    email: 'jordan@example.com',
    preferences: { language: 'en', timezone: 'America/New_York' },
  },
  {
    user_id: 'user_2',
    name: 'Casey',
    email: 'casey@example.com',
    preferences: { language: 'en', timezone: 'Europe/London' },
  },
];

export const DOCTORS: DoctorSeed[] = [
  {
    doctor_id: 'dr_khan',
    name: 'Dr. Amina Khan',
    specialty: 'General Practice',
    working_days: [1, 2, 3, 4, 5],
    start_hour: 9,
    end_hour: 17,
    slot_minutes: 30,
  },
  {
    doctor_id: 'dr_lee',
    name: 'Dr. Daniel Lee',
    specialty: 'Pediatrics',
    working_days: [1, 3, 5],
    start_hour: 10,
    end_hour: 16,
    slot_minutes: 30,
  },
  {
    doctor_id: 'dr_patel',
    name: 'Dr. Priya Patel',
    specialty: 'Dermatology',
    working_days: [2, 4, 6],
    start_hour: 9,
    end_hour: 13,
    slot_minutes: 20,
  },
];

export const KNOWLEDGE_INPUTS: KnowledgeSeed[] = [
  {
    title: 'Opening hours',
    content:
      'Sunrise Clinic is open Monday to Friday from 9 AM to 5 PM and on ' +
      'Saturday from 9 AM to 1 PM. The clinic is closed on Sundays and ' +
      'public holidays.',
    category: 'clinic',
  },
  {
    title: 'Address and parking',
    content:
      'Sunrise Clinic is at 120 Main Street, second floor. Free patient ' +
      'parking is available behind the building. The entrance is ' +
      'wheelchair accessible and there is an elevator.',
    category: 'clinic',
  },
  {
    title: 'Accepted insurance',
    content:
      'We accept Aetna, Blue Cross Blue Shield, Cigna, UnitedHealthcare, ' +
      'and Medicare. Patients without insurance can pay at the visit; a ' +
      'standard consultation costs 120 dollars.',
    category: 'billing',
  },
  {
    title: 'What to bring to an appointment',
    content:
      'Please bring a photo ID, your insurance card, a list of current ' +
      'medications, and any recent test results. Arrive 10 minutes early ' +
      'on your first visit to complete paperwork.',
    category: 'visits',
  },
  {
    title: 'Cancellation policy',
    content:
      'Please cancel or reschedule at least 24 hours before your ' +
      'appointment. Late cancellations and missed appointments may be ' +
      'charged a 25 dollar fee.',
    category: 'policy',
  },
  {
    title: 'Emergencies',
    content:
      'The clinic does not handle emergencies. For chest pain, trouble ' +
      'breathing, severe bleeding, or any life-threatening situation, ' +
      'call 911 or go to the nearest emergency room immediately.',
    category: 'safety',
  },
  {
    title: 'Prescription refills',
    content:
      'Refill requests are handled by your doctor within two business ' +
      'days. Some medications need a follow-up visit before they can be ' +
      'refilled.',
    category: 'visits',
  },
  {
    title: 'Test results',
    content:
      'Lab results are usually ready within three to five business days. ' +
      'Your doctor will call you to discuss any abnormal results. The ' +
      'receptionist cannot read or interpret results over the phone.',
    category: 'visits',
  },
];
