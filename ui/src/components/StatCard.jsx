function StatCard({
  label,
  value,
  description,
  variant = "",
}) {
  return (
    <div
      className={`stat-card ${variant}`}
    >
      <div className="stat-card-label">
        {label}
      </div>

      <div className="stat-card-value">
        {value}
      </div>

      {description && (
        <div className="stat-card-description">
          {description}
        </div>
      )}
    </div>
  );
}

export default StatCard;
