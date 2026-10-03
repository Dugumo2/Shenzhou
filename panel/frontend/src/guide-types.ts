export interface GuideSection {
  title: string
  paragraphs: string[]
  steps: string[]
  notes: string[]
}

export interface TaskGuide {
  id: string
  title: string
  summary: string
  category: string
  clients: string[]
  keywords: string[]
  sections: GuideSection[]
  verification: {
    state: 'not_tested' | 'not_applicable' | 'unsupported'
    checked_at: string | null
    note: string
  }
  versions: {
    software: string | null
    core: string | null
    historical: { software: string | null; core: string | null; date: string; note: string }[]
  }
}

export interface GuideCatalog {
  articles: TaskGuide[]
  query: string
  client: string | null
  total: number
  content_revision: string
  client_filters: { id: string; label: string }[]
}
