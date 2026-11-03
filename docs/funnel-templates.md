# LyteKeepr funnel templates (47)

Agreed 2026-11-03: drafted by GPT, checked by Claude, accepted by Peter. Built: lk_calendar/funnel_templates.json (made from this file). How they move contacts automatically:
`docs/funnel-automation.md`. Stage names are defaults: users can rename or skip them; the engine goes by each stage's role, not its
name. "Won / Closed Lost" are two alternative endings, not a sequence.

## Choosing a funnel (for Claude)

Choose by the sales process the user wants to manage, not only their industry. If their main CRM work is finding prospects by cold
outreach and following up, choose #1 whatever the industry. Otherwise choose the most fitting specialised process. When the
description doesn't make the sales process clear, choose the most likely business-specific default rather than assuming outbound.

## Booked stages (changed 2026-11-03)

LyteKeepr sees a booking in the calendar but can't tell that a meeting happened, so 18 templates (1, 3, 4, 5, 8, 9, 10, 11, 13, 17,
18, 20, 21, 22, 25, 26, 28, 38) use "... Booked" stages instead of "... Held / Viewed". A matching calendar booking moves a contact to
the funnel's Booked stage; it never moves anyone to a completed, approved, signed or won stage. Completion stages (e.g. #14 Property
Assessed, #19 Problem Assessed, #24 Assessment Completed, #27 Trial Completed, #30 Tour Completed) are moved by the user or Claude.

## Automation pattern per template

| Pattern | Templates |
|---|---|
| P1 Outbound Sales | 1 |
| P2 Consultation Sales | 3, 4, 5, 8, 9, 17, 18, 26 |
| P3 Appointment Bookings | 23, 24, 27, 28, 30, 31, 33 |
| P4 Milestone Sales | 2, 6, 7, 10, 11, 12, 13, 14, 15, 16, 19, 20, 21, 22, 25, 29, 32, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47 |

## Outbound sales and professional services

1. **Outbound Prospecting & Sales**: B2B agency founder, independent consultant, outbound SaaS salesperson
   Stages: Prospect Identified → First Contact → Conversation Started → Lead Qualified → Meeting Booked → Proposal Sent → Deal Won / Closed Lost
   Won: Deal Won. Lost: Closed Lost.
   Recognise: They primarily find prospects and initiate contact through LinkedIn, cold email, direct messages or phone calls, then manage conversations and follow-ups.

2. **Scoped Project Sales**: management consultant, freelance copywriter, graphic design studio
   Stages: New Lead → Needs Discussed → Scope Defined → Proposal Sent → Project Approved / Closed Lost
   Won: Project Approved. Lost: Closed Lost.
   Recognise: They sell customised, one-time professional projects where scoping, quoting and client approval are the main sales steps.

3. **Monthly Retainer Sales**: marketing agency, bookkeeping firm, virtual assistant service
   Stages: New Lead → Discovery Booked → Needs Defined → Scope Agreed → Proposal Sent → Retainer Signed / Closed Lost
   Won: Retainer Signed. Lost: Closed Lost.
   Recognise: They acquire clients for recurring monthly services and mainly track consultations, proposals and retainer agreements rather than cold outreach.

4. **Professional Client Engagements**: family lawyer, immigration consultant, tax advisory practice
   Stages: New Inquiry → Fit Checked → Consultation Booked → Fees Agreed → Engagement Signed / Closed Lost
   Won: Engagement Signed. Lost: Closed Lost.
   Recognise: They provide regulated or specialised professional services requiring intake, consultation, fee agreements or formal engagement.

5. **Custom Software Project Sales**: app development studio, website agency, automation consultancy
   Stages: New Lead → Requirements Call Booked → Scope Documented → Estimate Sent → Technical Review → Contract Signed / Closed Lost
   Won: Contract Signed. Lost: Closed Lost.
   Recognise: They build custom software, websites or integrations where technical requirements and estimates decide the sale.

6. **Managed IT Service Contracts**: managed IT provider, cybersecurity service company, outsourced IT firm
   Stages: New Lead → Systems Reviewed → Needs Identified → Service Plan → Proposal Sent → Agreement Signed / Closed Lost
   Won: Agreement Signed. Lost: Closed Lost.
   Recognise: They sell recurring technology support or security services after evaluating a client's systems.

7. **Corporate Training Bookings**: corporate trainer, leadership workshop provider, workplace safety instructor
   Stages: New Inquiry → Training Brief → Program Agreed → Quote Sent → Date Reserved → Booking Signed / Closed Lost
   Won: Booking Signed. Lost: Closed Lost.
   Recognise: Organisations hire them to deliver specific employee training, seminars or workshops.

8. **Demo-Led SaaS Sales**: small-business CRM, practice management software, customer support platform
   Stages: New Lead → Lead Qualified → Demo Booked → Product Evaluated → Pricing Sent → Subscription Started / Closed Lost
   Won: Subscription Started. Lost: Closed Lost.
   Recognise: They sell subscription software through conversations, demos, evaluations and pricing discussions.

9. **Enterprise Software Procurement**: enterprise HR platform, cybersecurity suite, procurement software provider
   Stages: New Lead → Discovery Booked → Demo Booked → Technical Review → Procurement Review → Contract Signed / Closed Lost
   Won: Contract Signed. Lost: Closed Lost.
   Recognise: They sell complex technology with several decision-makers, technical validation, purchasing approval or contract negotiation.

10. **Recruiting / Staffing Placements**: recruitment agency, executive search firm, specialist staffing company
    Stages: New Employer → Hiring Brief → Terms Agreed → Candidates Presented → Interviews Booked → Placement Confirmed / Closed Lost
    Won: Placement Confirmed. Lost: Closed Lost.
    Recognise: Employers pay them to find and place candidates; they manage hiring assignments through candidate presentation and placement.

## Real estate, finance and business opportunities

11. **Real Estate Buyer Transactions**: residential buyer's agent, condominium realtor, investment-property broker
    Stages: New Buyer → Needs Discussed → Financing Checked → Viewings Booked → Offer Accepted → Purchase Closed / Closed Lost
    Won: Purchase Closed. Lost: Closed Lost.
    Recognise: They represent people buying homes or investment properties, through search, offers and closing.

12. **Real Estate Seller Transactions**: listing agent, residential realtor, property sales broker
    Stages: New Seller → Property Valued → Listing Signed → Offers Reviewed → Offer Accepted → Sale Closed / Closed Lost
    Won: Sale Closed. Lost: Closed Lost.
    Recognise: They represent owners selling property and manage listings through offers and completed sales.

13. **Commercial Leasing Deals**: office leasing broker, retail leasing agent, commercial tenant representative
    Stages: New Client → Space Needs → Tours Booked → Offer Submitted → Lease Negotiated → Lease Signed / Closed Lost
    Won: Lease Signed. Lost: Closed Lost.
    Recognise: They help businesses lease offices, retail space, warehouses or other commercial property.

14. **Property Management Contracts**: residential property manager, short-term rental manager, commercial property manager
    Stages: New Owner → Property Assessed → Services Discussed → Fee Proposal → Management Signed / Closed Lost
    Won: Management Signed. Lost: Closed Lost.
    Recognise: Owners hire them to handle rentals, tenants, maintenance and day-to-day operations.

15. **Mortgage / Loan Applications**: residential mortgage broker, commercial loan broker, business financing broker
    Stages: New Applicant → Financial Review → Financing Options → Application Submitted → Lender Approved → Loan Funded / Closed Lost
    Won: Loan Funded. Lost: Closed Lost.
    Recognise: They arrange financing through lenders and track applications from qualification to funding.

16. **Insurance Policy Sales**: life insurance broker, home insurance agent, commercial insurance broker
    Stages: New Lead → Coverage Reviewed → Quotes Prepared → Options Discussed → Policy Issued / Closed Lost
    Won: Policy Issued. Lost: Closed Lost.
    Recognise: They assess insurance needs, compare coverage and help customers buy policies.

17. **Financial Advisory Client Acquisition**: wealth manager, investment advisor, retirement planner
    Stages: New Prospect → Intro Booked → Goals Reviewed → Plan Presented → Client Signed / Closed Lost
    Won: Client Signed. Lost: Closed Lost.
    Recognise: They win clients for financial planning, investment management or ongoing advice.

18. **Franchise / Licensing Sales**: franchise company, business licensor, dealership network
    Stages: New Inquiry → Candidate Screened → Intro Booked → Business Reviewed → Terms Reviewed → Franchise Signed / Closed Lost
    Won: Franchise Signed. Lost: Closed Lost.
    Recognise: They recruit people or companies to buy franchises, operating rights, licences or territories.

## Trades, home services and repairs

19. **Quoted Repair / Installation Sales**: plumbing contractor, auto repair shop, computer repair business
    Stages: New Request → Problem Assessed → Estimate Sent → Work Approved / Closed Lost
    Won: Work Approved. Lost: Closed Lost.
    Recognise: Customers describe a problem, get an assessment or estimate, and decide whether to authorise the work.

20. **Construction / Renovation Contracts**: general contractor, kitchen renovation company, commercial remodelling firm
    Stages: New Lead → Site Visit Booked → Scope Agreed → Estimate Sent → Terms Negotiated → Contract Signed / Closed Lost
    Won: Contract Signed. Lost: Closed Lost.
    Recognise: They sell substantial building or renovation projects with site visits, detailed estimates and formal contracts.

21. **Technical Home Installation Sales**: solar installer, heat-pump company, home energy systems provider
    Stages: New Lead → Home Qualified → Survey Booked → System Designed → Financing Reviewed → Contract Signed / Closed Lost
    Won: Contract Signed. Lost: Closed Lost.
    Recognise: They sell expensive installed home systems needing property evaluation, technical design or financing.

22. **Recurring Property Service Contracts**: commercial cleaning business, landscaping company, pest control provider
    Stages: New Inquiry → Site Visit Booked → Service Plan → Quote Sent → Service Agreed / Closed Lost
    Won: Service Agreed. Lost: Closed Lost.
    Recognise: They win customers for recurring cleaning, landscaping, maintenance or treatment services.

## Personal services, health, wellness and education

23. **Personal Service Appointment Sales**: hair salon, massage therapist, pet groomer
    Stages: New Inquiry → Service Chosen → Availability Checked → Appointment Confirmed / Closed Lost
    Won: Appointment Confirmed. Lost: Closed Lost.
    Recognise: They turn customer inquiries into booked individual service appointments.

24. **Clinical Treatment Plan Sales**: orthodontic clinic, physiotherapy practice, elective dental clinic
    Stages: New Patient → Consultation Booked → Assessment Completed → Treatment Proposed → Plan Accepted / Closed Lost
    Won: Plan Accepted. Lost: Closed Lost.
    Recognise: Patients have a consultation or assessment before deciding on a proposed treatment plan.

25. **Personal Care Service Contracts**: senior home care agency, disability support provider, private caregiving service
    Stages: New Inquiry → Needs Discussed → Care Assessment Booked → Plan Proposed → Agreement Signed / Closed Lost
    Won: Agreement Signed. Lost: Closed Lost.
    Recognise: Individuals or families arrange ongoing care after discussing needs and reviewing a care plan.

26. **Coaching Package Sales**: business coach, life coach, nutrition coach
    Stages: New Lead → Intro Booked → Goals Defined → Program Offered → Program Purchased / Closed Lost
    Won: Program Purchased. Lost: Closed Lost.
    Recognise: They sell structured coaching programmes after an introductory conversation about goals.

27. **Fitness Trial-to-Membership Sales**: personal training studio, boutique gym, group fitness studio
    Stages: New Lead → Trial Booked → Trial Completed → Plan Offered → Membership Started / Closed Lost
    Won: Membership Started. Lost: Closed Lost.
    Recognise: They turn prospects into members or training clients after a trial, introductory workout or consultation.

28. **Private Lesson Bookings**: mathematics tutor, music teacher, language instructor
    Stages: New Inquiry → Needs Discussed → Trial Booked → Package Offered → Lessons Booked / Closed Lost
    Won: Lessons Booked. Lost: Closed Lost.
    Recognise: They teach individuals through private or small-group lessons and sell recurring lesson packages.

29. **Course / Program Enrollment**: online course creator, coding bootcamp, vocational training provider
    Stages: New Lead → Course Selected → Questions Answered → Registration Started → Enrollment Paid / Closed Lost
    Won: Enrollment Paid. Lost: Closed Lost.
    Recognise: They sell structured courses, cohorts or training programmes to individual learners.

30. **School / Childcare Admissions**: private school, preschool, daycare centre
    Stages: New Inquiry → Tour Booked → Tour Completed → Application Reviewed → Place Offered → Enrollment Signed / Closed Lost
    Won: Enrollment Signed. Lost: Closed Lost.
    Recognise: Families apply for admission, often with tours, applications, acceptance and paperwork.

## Events, hospitality, travel and rentals

31. **Date-Based Professional Bookings**: wedding photographer, event DJ, keynote speaker
    Stages: New Inquiry → Date Checked → Package Discussed → Quote Sent → Booking Confirmed / Closed Lost
    Won: Booking Confirmed. Lost: Closed Lost.
    Recognise: They are hired for a specific service on a particular date, usually with a booking agreement or deposit.

32. **Custom Event Service Contracts**: wedding planner, event caterer, event decoration company
    Stages: New Inquiry → Event Brief → Plan Prepared → Proposal Sent → Contract Signed / Closed Lost
    Won: Contract Signed. Lost: Closed Lost.
    Recognise: They sell customised event services where planning, scope and proposals win the client.

33. **Venue / Group Hospitality Bookings**: wedding venue, restaurant private-dining room, hotel group-booking service
    Stages: New Inquiry → Date Checked → Space Reviewed → Package Quoted → Booking Confirmed / Closed Lost
    Won: Booking Confirmed. Lost: Closed Lost.
    Recognise: Customers reserve venues, private dining, group accommodation or event facilities.

34. **Custom Travel Bookings**: travel advisor, luxury vacation planner, private tour operator
    Stages: New Inquiry → Trip Discussed → Itinerary Prepared → Options Reviewed → Booking Confirmed / Closed Lost
    Won: Booking Confirmed. Lost: Closed Lost.
    Recognise: They design customised itineraries or tours and guide clients through choosing and booking.

35. **Equipment / Item Rental Bookings**: event equipment rental, camera rental business, construction equipment rental firm
    Stages: New Inquiry → Dates Checked → Items Selected → Quote Sent → Rental Confirmed / Closed Lost
    Won: Rental Confirmed. Lost: Closed Lost.
    Recognise: They rent equipment or items for set periods and manage availability, pricing and reservations.

## Retail, products, wholesale and manufacturing

36. **Conversational Online Product Sales**: Instagram boutique, WhatsApp product seller, handmade goods business
    Stages: New Inquiry → Product Selected → Questions Answered → Payment Link → Order Paid / Closed Lost
    Won: Order Paid. Lost: Closed Lost.
    Recognise: They sell physical products through messages and follow-ups rather than relying only on automated checkout.

37. **Consultative Consumer Product Sales**: luxury furniture showroom, custom jewellery retailer, premium bicycle shop
    Stages: New Inquiry → Needs Discussed → Options Shown → Quote Sent → Order Confirmed / Closed Lost
    Won: Order Confirmed. Lost: Closed Lost.
    Recognise: They sell expensive or customised consumer products where buyers need advice, selection or a personal quote.

38. **Vehicle Dealership Sales**: used car dealership, motorcycle dealer, recreational vehicle dealer
    Stages: New Lead → Vehicle Chosen → Test Drive Booked → Price Agreed → Terms Agreed → Sale Signed / Closed Lost
    Won: Sale Signed. Lost: Closed Lost.
    Recognise: They sell vehicles through consultations, viewings or test drives, negotiation and purchase agreements.

39. **B2B Equipment / Product Sales**: industrial machinery supplier, commercial copier dealer, medical equipment seller
    Stages: New Account → Needs Discussed → Options Reviewed → Quote Sent → Order Received / Closed Lost
    Won: Order Received. Lost: Closed Lost.
    Recognise: They sell business equipment or products through conversations, specifications, quotes and purchase orders.

40. **Wholesale Account Sales**: food wholesaler, clothing distributor, beauty-products wholesaler
    Stages: New Retailer → Account Qualified → Catalog Sent → Pricing Agreed → Samples Reviewed → First Order / Closed Lost
    Won: First Order. Lost: Closed Lost.
    Recognise: They open new accounts with retailers or resellers who buy in bulk.

41. **Custom Manufacturing Orders**: packaging manufacturer, metal fabrication shop, custom furniture manufacturer
    Stages: New Inquiry → Specs Confirmed → Design Reviewed → Quote Sent → Order Approved / Closed Lost
    Won: Order Approved. Lost: Closed Lost.
    Recognise: They make goods to order from customer specifications, designs or drawings.

42. **Government Tender / RFP Sales**: government IT contractor, public construction bidder, institutional equipment supplier
    Stages: Tender Found → Eligibility Checked → Bid Prepared → Bid Submitted → Contract Awarded / Closed Lost
    Won: Contract Awarded. Lost: Closed Lost.
    Recognise: They win business through formal tenders, public procurement, institutional bidding or RFPs.

43. **Freight / Logistics Bookings**: freight broker, shipping agent, third-party logistics provider
    Stages: New Shipper → Shipment Details → Rate Quoted → Terms Accepted → Shipment Booked / Closed Lost
    Won: Shipment Booked. Lost: Closed Lost.
    Recognise: They arrange shipments and win customers through rate quotes and booking confirmations.

## Nonprofits, sponsorships and memberships

44. **Major Donor Fundraising**: charitable foundation, community charity, educational nonprofit
    Stages: Donor Identified → Contact Made → Interest Confirmed → Gift Requested → Donation Committed / Closed Lost
    Won: Donation Committed. Lost: Closed Lost.
    Recognise: They build donor relationships and ask for contributions or pledges.

45. **Grant Application Pipeline**: arts nonprofit, community development organisation, research foundation
    Stages: Grant Found → Eligibility Checked → Application Prepared → Application Submitted → Grant Awarded / Closed Lost
    Won: Grant Awarded. Lost: Closed Lost.
    Recognise: They seek funding by applying to foundations, governments or other grant-makers.

46. **Sponsorship / Brand Partnership Sales**: conference organiser, creator sponsorship agency, community sports club
    Stages: Sponsor Identified → Contact Made → Package Presented → Terms Negotiated → Sponsorship Signed / Closed Lost
    Won: Sponsorship Signed. Lost: Closed Lost.
    Recognise: They sell brand exposure, promotional placements, sponsorship packages or commercial partnerships.

47. **Paid Membership Sales**: professional association, private business club, paid networking community
    Stages: New Prospect → Benefits Discussed → Membership Chosen → Application Approved → Membership Paid / Closed Lost
    Won: Membership Paid. Lost: Closed Lost.
    Recognise: They recruit paying members to organisations, associations, clubs or private communities.
